import torch
import torch.nn as nn
import torch.nn.functional as F
import math

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class Attention(nn.Module):
    def __init__(self, feature_dim):
        super(Attention, self).__init__()

        self.attn_1 = nn.Linear(feature_dim, feature_dim)
        self.attn_2 = nn.Linear(feature_dim, 1)

        # inititalize
        nn.init.xavier_uniform_(self.attn_1.weight)
        nn.init.xavier_uniform_(self.attn_2.weight)
        self.attn_1.bias.data.fill_(0.0)
        self.attn_2.bias.data.fill_(0.0)

    def forward(self, x, return_attention=False):
        """
        Input x is encoder output
        return_attention decides whether to return
        attention scores over the encoder output
        """
        sequence_length = x.shape[1]

        self_attention_scores = self.attn_2(torch.tanh(self.attn_1(x)))

        # Attend for each time step using the previous context
        context_vectors = []
        attention_vectors = []

        for t in range(sequence_length):
            # For each timestep the context that is attented grows
            # as there are more available previous hidden states
            weighted_attention_scores = F.softmax(
                self_attention_scores[:, :t + 1, :].clone(), dim=1)

            context_vectors.append(
                torch.sum(weighted_attention_scores * x[:, :t + 1, :].clone(), dim=1))

            if return_attention:
                attention_vectors.append(
                    weighted_attention_scores.cpu().detach().numpy())

        context_vectors = torch.stack(context_vectors).transpose(0, 1)

        return context_vectors, attention_vectors


class PositionalAttention(nn.Module):
    def __init__(self,
                 feature_dim,
                 positioning_embedding=20,
                 num_building_blocks=3):
        super(PositionalAttention, self).__init__()
        self.num_building_blocks = num_building_blocks

        self.positioning_generator = nn.LSTM(
            feature_dim, positioning_embedding, batch_first=True)

        self.sigma_generator = nn.Linear(positioning_embedding, 1)
        self.mu_generator = nn.Linear(
            positioning_embedding, num_building_blocks)

    def flatten_parameters(self):
        """
        Flatten parameters of all reccurrent components in the model.
        """
        self.positioning_generator.flatten_parameters()

    @staticmethod
    def normal_pdf(x, mu, sigma):
        """Return normalized Gaussian_pdf(x)."""
        x = torch.exp(-(x - mu)**2 / (2 * sigma**2 +10e-4))
        return x

    def forward(self, x, pad_lengths, return_attention=False):
        """
        Input x is encoder output
        return_attention decides whether to return
        attention scores over the encoder output
        """

        batch_size = x.shape[0]
        sequence_length = x.shape[1]

        # Need the lengths to normalize each sentence to respective length
        # for the building blocks - 1/N and j/N
        lengths = pad_lengths.expand(sequence_length, batch_size).type(torch.float)

        positioning_weights, _ = self.positioning_generator(x)
        mu_weights = F.relu(self.mu_generator(positioning_weights))
        sigma_weights = torch.sigmoid(
            self.sigma_generator(positioning_weights))

        prev_mu = torch.zeros(batch_size, device=device)
        building_blocks = torch.ones(
            (sequence_length, batch_size, self.num_building_blocks), device=device)
        building_blocks[:, :, 1] = 1/lengths
        building_blocks[:, :, 2] = (torch.arange(
            sequence_length, dtype=torch.float, device=device)+1).unsqueeze(1).expand(-1, batch_size) / lengths

        # Attend for each time step using the previous context
        position_vectors = []  # Which positions to attend to
        attention_vectors = []

        # we go over the whole sequence - even though it is padded so the max
        # length might be shorter.
        for j in range(sequence_length):
            # For each timestep the context that is attented grows
            # as there are more available previous hidden states
            bb = building_blocks[j].clone()
            bb[:, 0] = prev_mu

            mu = torch.bmm(mu_weights[:, j, :].clone().unsqueeze(
                1), bb.unsqueeze(2)).squeeze(1)
            prev_mu = mu.squeeze()

            sigma = sigma_weights[:, j, :]

            # relative counter that represents 0-1 where to attend on sequence up till now
            rel_counter = torch.arange(
                j+1, dtype=torch.float, device=device).unsqueeze(0) / (j+1)

            gaussian_weighted_attention = self.normal_pdf(
                rel_counter.expand(batch_size, -1), mu, sigma).unsqueeze(2)

            gaussian_weighted_attention = F.normalize(
                gaussian_weighted_attention[:, :j+1, :].clone(), p=1)
            # multiply the weights with the hidden encoded states found till this point
            applied_positional_attention = x[:, :j+1,
                                             :].clone() * gaussian_weighted_attention
            position_vectors.append(
                torch.sum(applied_positional_attention, dim=1))

            if return_attention:
                attention_vectors.append(
                    gaussian_weighted_attention.cpu().detach().numpy())

        context_vectors = torch.stack(position_vectors).transpose(0, 1)

        return context_vectors, attention_vectors


class AttentiveRNNLanguageModel(nn.Module):
    """
    Implements an Attentive Language Model according to http://www.aclweb.org/anthology/I17-1045
    """

    def __init__(self, vocab_size,
                 embedding_size=65,
                 hidden_size=65,
                 n_layers=1,
                 dropout_p_input=0.5,
                 dropout_p_encoder=0.0,
                 dropout_p_decoder=0.5,
                 attention=False,
                 positional_attention=True,
                 positioning_embedding=20,
                 tie_weights=True):

        super(AttentiveRNNLanguageModel, self).__init__()

        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.n_layers = n_layers
        self.vocab_size = vocab_size
        self.attention = attention
        self.positional_attention = positional_attention

        self.input_dropout = nn.Dropout(dropout_p_input)
        self.embedding = nn.Embedding(vocab_size, embedding_size)
        self.encoder = nn.LSTM(embedding_size, hidden_size,
                               n_layers, batch_first=True,
                               dropout=dropout_p_encoder)
        if self.attention:
            self.attention_score_module = Attention(hidden_size)
        if self.positional_attention:
            self.position_score_module = PositionalAttention(
                hidden_size, positioning_embedding=positioning_embedding)

        # concatenation FF Layer to combine context and prev output
        if self.attention or self.positional_attention:
            self.concatenation_layer = nn.Linear(hidden_size * 2, hidden_size)

        if self.attention and self.positional_attention:
            raise NotImplementedError(
                "Attention and Positional Attention cannot be both activated")

        self.decoder = nn.Linear(hidden_size, vocab_size)
        self.decoder_dropout = nn.Dropout(dropout_p_decoder)

        # Optionally tie weights as in:
        # "Using the Output Embedding to Improve Language Models" (Press & Wolf 2016)
        # https://arxiv.org/abs/1608.05859
        # and
        # "Tying Word Vectors and Word Classifiers: A Loss Framework for Language Modeling" (Inan et al. 2016)
        # https://arxiv.org/abs/1611.01462
        if tie_weights:
            if self.embedding_size != hidden_size:
                raise ValueError(
                    'When using the tied flag, encoder embedding_size must be equal to hidden_size')
            self.decoder.weight = self.embedding.weight

        self.init_weights()

    def forward(self, input, pad_lengths, return_attention=False):

        embedded = self.embedding(input)
        embedded = self.input_dropout(embedded)

        encoder_output, _ = self.encoder(embedded)

        if self.attention:
            context_vectors, attention_score = self.attention_score_module(
                encoder_output, return_attention=return_attention)

        if self.positional_attention:
            context_vectors, attention_score = self.position_score_module(
                encoder_output, pad_lengths, return_attention=return_attention)

        if self.attention or self.positional_attention:
            combined_encoding = torch.cat(
                (context_vectors, encoder_output), dim=2)
            # concatenation layer
            encoder_output = torch.tanh(
                self.concatenation_layer(combined_encoding))

        output = self.decoder_dropout(encoder_output)
        decoded = self.decoder(output.contiguous())

        if return_attention:
            return decoded, attention_score

        return decoded

    def flatten_parameters(self):
        """
        Flatten parameters of all reccurrent components in the model.
        """
        self.encoder.flatten_parameters()
        if self.positional_attention:
            self.position_score_module.flatten_parameters()

    def init_weights(self, init_range=0.1):
        """
        Standard weight initialization
        """
        self.embedding.weight.data.uniform_(
            -init_range, init_range)
        self.decoder.bias.data.zero_()
        self.decoder.weight.data.uniform_(-init_range, init_range)

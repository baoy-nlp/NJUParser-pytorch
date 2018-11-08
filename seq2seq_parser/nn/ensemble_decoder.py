import random

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from seq2seq_parser.nn.attention import BothSideAttention
from seq2seq_parser.nn.attention import DotProductAttention as Attention
from seq2seq_parser.nn.baseRNN import BaseRNN
from seq2seq_parser.nn.rnn_stack import RNNStack
from seq2seq_parser.utils.global_names import GlobalNames
from seq2seq_parser.utils.ops import reflect
from seq2seq_parser.utils.stack import GrammarStack
from seq2seq_parser.utils.stack import LengthInfo
from seq2seq_parser.utils.stack import ParentStack

if GlobalNames.use_fnn_attention:
    from seq2seq_parser.nn.attention import FNNAttention as Attention


class DecoderRNN(BaseRNN):
    r"""
    Provides functionality for decoding in a seq2seq framework, with an option for attention.

    Args:
        vocab_size (int): size of the vocabulary
        max_len (int): a maximum allowed length for the sequence to be processed
        hidden_size (int): the number of features in the hidden state `h`
        sos_id (int): index of the start of sentence symbol
        eos_id (int): index of the end of sentence symbol
        n_layers (int, optional): number of recurrent layers (default: 1)
        rnn_cell (str, optional): type of RNN cell (default: gru)
        bidirectional (bool, optional): if the encoder is bidirectional (default False)
        input_dropout_p (float, optional): dropout probability for the input sequence (default: 0)
        dropout_p (float, optional): dropout probability for the output sequence (default: 0)
        use_attention(bool, optional): flag indication whether to use attention mechanism or not (default: false)

    Attributes:
        KEY_ATTN_SCORE (str): key used to indicate attention weights in `ret_dict`
        KEY_LENGTH (str): key used to indicate a list representing lengths of output sequences in `ret_dict`
        KEY_SEQUENCE (str): key used to indicate a list of sequences in `ret_dict`

    Inputs: inputs, encoder_hidden, encoder_outputs, function, teacher_forcing_ratio
        - **inputs** (batch, seq_len, input_size): list of sequences, whose length is the batch size and within which
          each sequence is a list of token IDs.  It is used for teacher forcing when provided. (default `None`)
        - **encoder_hidden** (num_layers * num_directions, batch_size, hidden_size): tensor containing the features in the
          hidden state `h` of encoder. Used as the initial hidden state of the decoder. (default `None`)
        - **encoder_outputs** (batch, seq_len, hidden_size): tensor with containing the outputs of the encoder.
          Used for attention mechanism (default is `None`).
        - **function** (torch.nn.Module): A function used to generate symbols from RNN hidden state
          (default is `torch.nn.functional.log_softmax`).
        - **teacher_forcing_ratio** (float): The probability that teacher forcing will be used. A random number is
          drawn uniformly from 0-1 for every decoding token, and if the sample is smaller than the given value,
          teacher forcing would be used (default is 0).

    Outputs: decoder_outputs, decoder_hidden, ret_dict
        - **decoder_outputs** (seq_len, batch, vocab_size): list of tensors with size (batch_size, vocab_size) containing
          the outputs of the decoding function.
        - **decoder_hidden** (num_layers * num_directions, batch, hidden_size): tensor containing the last hidden
          state of the decoder.
        - **ret_dict**: dictionary containing additional information as follows {*KEY_LENGTH* : list of integers
          representing lengths of output sequences, *KEY_SEQUENCE* : list of sequences, where each sequence is a list of
          predicted token IDs }.
    """

    KEY_ATTN_SCORE = 'attention_score'
    KEY_LENGTH = 'length'
    KEY_SEQUENCE = 'sequence'

    def __init__(self,
                 vocab,
                 max_len,
                 input_size,
                 hidden_size,
                 eos_id,
                 sos_id,
                 n_layers=1,
                 rnn_cell='gru',
                 bidirectional=False,
                 input_dropout_p=0,
                 dropout_p=0,
                 use_attention=False):
        super(DecoderRNN, self).__init__(vocab, max_len, input_size, hidden_size,
                                         input_dropout_p, dropout_p,
                                         n_layers, rnn_cell)

        model_info = """
        this is ensemble framework contains:\n
        grammar choice:{}\n
        parent choice:{}\n
        det attention choice:{}\n
        biDet attention choice:{}\n
        constrain choice:{}\n
        """.format(
            GlobalNames.use_grammar,
            GlobalNames.use_parent,
            GlobalNames.use_det,
            GlobalNames.use_biatt,
            GlobalNames.use_constrain
        )

        print(model_info)

        self.bidirectional_encoder = bidirectional
        if GlobalNames.use_ts:
            self.rnn = RNNStack(self.rnn_cell, input_size, hidden_size, n_layers, dropout_p)
        else:
            self.rnn = self.rnn_cell(input_size, hidden_size, n_layers, batch_first=True, dropout=dropout_p)

        self.max_length = max_len
        self.use_attention = use_attention
        self.eos_id = eos_id
        self.sos_id = sos_id

        self.init_input = None
        if GlobalNames.use_grammar or GlobalNames.use_parent:
            self.grammar_embed = nn.Embedding(vocab, hidden_size)
            if GlobalNames.use_grammar_rnn:
                self.grammar_rnn = torch.nn.GRU(hidden_size, hidden_size, num_layers=1, batch_first=True)

        if use_attention:
            if GlobalNames.use_biatt:
                self.attention = BothSideAttention(self.hidden_size)
            else:
                self.attention = Attention(self.hidden_size)

        feature_size = 1
        if GlobalNames.use_grammar:
            feature_size += 1
        if GlobalNames.use_parent:
            feature_size += 1

        self.soft_input_size = self.hidden_size * feature_size + self.input_size
        self.out = nn.Linear(self.soft_input_size, self.vocab_size)

    def init_stack(self, input_lengths):
        if GlobalNames.use_det or GlobalNames.use_biatt:
            self.length_info = LengthInfo(input_lengths)

        if GlobalNames.use_constrain or GlobalNames.use_parent:
            self.decoder_stack = ParentStack()
            if GlobalNames.use_length:
                length_info = LengthInfo(input_lengths)
                self.decoder_stack.length_info = length_info

        if GlobalNames.use_grammar:
            self.decoder_stack = GrammarStack()
            if GlobalNames.use_length:
                length_info = LengthInfo(input_lengths)
                self.decoder_stack.length_info = length_info

    def update_stack(self, input_var, batch_size, top_features):
        if GlobalNames.use_grammar:
            grammar_input = self.grammar_embed(input_var)
            if GlobalNames.use_grammar_add:
                if not self.decoder_stack.has_init:
                    self.decoder_stack.push(symbol=input_var, parent=grammar_input, cur=grammar_input)
                    grammar_context = grammar_input
                else:
                    top_hidden = self.decoder_stack.pop().squeeze().unsqueeze(1)
                    grammar_context = top_hidden + grammar_input
                    self.decoder_stack.push(symbol=input_var, parent=grammar_context, cur=grammar_input)
            else:
                if not self.decoder_stack.has_init:
                    self.decoder_stack.push(symbol=input_var, parent=grammar_input, cur=grammar_input)
                    grammar_context, new_hidden = self.grammar_rnn(grammar_input, grammar_input.view(1, batch_size, -1))
                else:
                    top_hidden = self.decoder_stack.pop()
                    grammar_context, new_hidden = self.grammar_rnn(grammar_input, top_hidden)
                    self.decoder_stack.push(symbol=input_var, parent=new_hidden, cur=grammar_input)

            top_features.append(grammar_context)

        if GlobalNames.use_parent:
            self.decoder_stack.push(input_var)
            cur_symbol = self.decoder_stack.top_symbol
            parent = self.grammar_embed(cur_symbol)
            top_features.append(parent)

        if not GlobalNames.use_parent and not GlobalNames.use_grammar and GlobalNames.use_constrain:
            self.decoder_stack.push(input_var)

        return top_features

    def forward_step(self, input_var, hidden, encoder_outputs, function):
        batch_size = input_var.size(0)
        output_size = input_var.size(1)
        embedded = self.embedding(input_var)
        embedded = self.input_dropout(embedded)

        # output = [cur_rnn_output]
        if GlobalNames.use_ts:
            output, hidden = self.rnn(input_var, embedded, hidden)
        else:
            output, hidden = self.rnn(embedded, hidden)

        top_features = [embedded]

        top_features = self.update_stack(input_var, batch_size, top_features)

        attn = None
        if self.use_attention:
            if GlobalNames.use_det:
                self.length_info.push(input_var)
                attent_context = self.length_info.extract_context(encoder_outputs)
                output, attn = self.attention.forward(output, attent_context)
            elif GlobalNames.use_biatt:
                self.length_info.push(input_var)
                attn_index = self.length_info.cur_site()
                output, attn = self.attention.forward(output, encoder_outputs, attn_index)
            else:
                output, attn = self.attention(output, encoder_outputs)
            top_features.append(output)

        output = torch.cat(top_features, dim=-1)
        predicted_softmax = function(self.out(output.contiguous().view(-1, self.soft_input_size)), dim=-1).view(batch_size, output_size, -1)
        return predicted_softmax, hidden, attn

    def forward(self, inputs=None, encoder_hidden=None, encoder_outputs=None,
                function=F.log_softmax, teacher_forcing_ratio=0, input_lengths=None):
        ret_dict = dict()

        if self.use_attention:
            ret_dict[DecoderRNN.KEY_ATTN_SCORE] = list()

        inputs, batch_size, max_length = self._validate_args(inputs, encoder_hidden, encoder_outputs,
                                                             function, teacher_forcing_ratio)
        decoder_hidden = self._init_state(encoder_hidden)

        self.init_stack(input_lengths)

        use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

        decoder_outputs = []
        sequence_symbols = []
        lengths = np.array([max_length] * batch_size)

        def decode(step, step_output, step_attn):
            decoder_outputs.append(step_output)
            if self.use_attention:
                ret_dict[DecoderRNN.KEY_ATTN_SCORE].append(step_attn)
            symbols = decoder_outputs[-1].topk(1)[1]
            sequence_symbols.append(symbols)

            eos_batches = symbols.data.eq(self.eos_id)
            if eos_batches.dim() > 0:
                eos_batches = eos_batches.cpu().view(-1).numpy()
                update_idx = ((lengths > step) & eos_batches) != 0
                lengths[update_idx] = len(sequence_symbols)
            return symbols

        def decode_with_constrain(step, step_output, step_attn, mask):
            if self.use_attention:
                ret_dict[DecoderRNN.KEY_ATTN_SCORE].append(step_attn)
            mask_output = F.softmax(step_output, -1) * mask
            symbols = mask_output.topk(1)[1]
            sequence_symbols.append(symbols)

            step_output = function(step_output, -1)
            decoder_outputs.append(step_output)
            eos_batches = symbols.data.eq(self.eos_id)
            if eos_batches.dim() > 0:
                eos_batches = eos_batches.cpu().view(-1).numpy()
                update_idx = ((lengths > step) & eos_batches) != 0
                lengths[update_idx] = len(sequence_symbols)
            return symbols

        if use_teacher_forcing:
            for di in range(inputs.size(1) - 1):
                decoder_input = inputs[:, di].unsqueeze(1)
                decoder_output, decoder_hidden, attn = self.forward_step(decoder_input, decoder_hidden, encoder_outputs,
                                                                         function=function)
                step_output = decoder_output.squeeze(1)
                decode(di, step_output, attn)
        else:
            decoder_input = inputs[:, 0].unsqueeze(1)

            for di in range(max_length):
                if GlobalNames.use_constrain:
                    decoder_output, decoder_hidden, step_attn = self.forward_step(decoder_input, decoder_hidden, encoder_outputs,
                                                                                  function=reflect)
                    step_output = decoder_output.squeeze(1)
                    mask = self.decoder_stack.mask
                    symbols = decode_with_constrain(di, step_output, step_attn, mask)
                    decoder_input = symbols
                else:
                    decoder_output, decoder_hidden, step_attn = self.forward_step(decoder_input, decoder_hidden, encoder_outputs,
                                                                                  function=function)
                    step_output = decoder_output.squeeze(1)
                    symbols = decode(di, step_output, step_attn)
                    decoder_input = symbols
        ret_dict[DecoderRNN.KEY_SEQUENCE] = sequence_symbols
        ret_dict[DecoderRNN.KEY_LENGTH] = lengths.tolist()
        return decoder_outputs, decoder_hidden, ret_dict

    def _init_state(self, encoder_hidden):
        """ Initialize the encoder hidden state. """
        if encoder_hidden is None:
            return None
        if isinstance(encoder_hidden, tuple):
            encoder_hidden = tuple([self._cat_directions(h) for h in encoder_hidden])
        else:
            encoder_hidden = self._cat_directions(encoder_hidden)
        return encoder_hidden

    def _cat_directions(self, h):
        """ If the encoder is bidirectional, do the following transformation.
            (#directions * #layers, #batch, hidden_size) -> (#layers, #batch, #directions * hidden_size)
        """
        if self.bidirectional_encoder:
            h = torch.cat([h[0:h.size(0):2], h[1:h.size(0):2]], 2)
        return h

    def _validate_args(self, inputs, encoder_hidden, encoder_outputs, function, teacher_forcing_ratio):
        if self.use_attention:
            if encoder_outputs is None:
                raise ValueError("Argument encoder_outputs cannot be None when attention is used.")

        # inference batch size
        if inputs is None and encoder_hidden is None:
            batch_size = 1
        else:
            if inputs is not None:
                batch_size = inputs.size(0)
            else:
                if self.rnn_cell is nn.LSTM:
                    batch_size = encoder_hidden[0].size(1)
                elif self.rnn_cell is nn.GRU:
                    batch_size = encoder_hidden.size(1)

        # set default input and max decoding length
        if inputs is None:
            if teacher_forcing_ratio > 0:
                raise ValueError("Teacher forcing has to be disabled (set 0) when no inputs is provided.")
            inputs = torch.LongTensor([self.sos_id] * batch_size).view(batch_size, 1)
            if torch.cuda.is_available():
                inputs = inputs.cuda()
            max_length = self.max_length
        else:
            if teacher_forcing_ratio > 0:
                max_length = inputs.size(1) - 1  # minus the start of sequence symbol
            else:
                max_length = self.max_length
        return inputs, batch_size, max_length

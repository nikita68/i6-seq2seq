import tensorflow as tf
from tensorflow.contrib.rnn import LSTMCell, LSTMStateTuple
from tensorflow.python.layers import core as layers_core
import numpy as np
import copy

# Implementation of the "A Neural Transducer" paper, Navdeep Jaitly et. al (2015): https://arxiv.org/abs/1511.04868

# NOTE: Time major

# Constants
input_dimensions = 1
vocab_size = 4
input_embedding_size = 20
encoder_hidden_units = 8
inputs_embedded = True
transducer_hidden_units = 8
batch_size = 1  # Cannot be increased, see paper
GO_SYMBOL = vocab_size - 1  # TODO: Make these constants correct
END_SYMBOL = vocab_size
E_SYMBOL = vocab_size - 2
input_block_size = 3
log_prob_init_value = 0
beam_width = 5  # For inference

# ---------------- Helper classes -----------------------

class Alignment(object):
    def __init__(self):
        self.alignment_position = (0, 1)  # x = position in target (y~), y = block index, both start at 1
        self.log_prob = log_prob_init_value  # The sum log prob of this alignment over the target indices
        self.alignment_locations = []  # At which indices in the target output we need to insert <e>
        self.last_state_transducer = np.zeros(shape=(2, 1, transducer_hidden_units))  # Transducer state

    def __compute_sum_probabilities(self, transducer_outputs, targets, transducer_amount_outputs):
        def get_prob_at_timestep(timestep):
            return np.log(transducer_outputs[timestep][0][targets[start_index + timestep]])

        start_index = self.alignment_position[0] - transducer_amount_outputs  # The current position of this alignment
        prob = log_prob_init_value
        for i in range(0, transducer_amount_outputs):
            prob += get_prob_at_timestep(i)
        return prob

    def insert_alignment(self, index, block_index, transducer_outputs, targets, transducer_amount_outputs,
                         new_transducer_state):
        """
        Inserts alignment properties for a new block.
        :param index: The index of of y~ corresponding to the last target index.
        :param block_index: The new block index.
        :param transducer_outputs: The computed transducer outputs.
        :param targets: The complete target array, should be of shape [total_target_length].
        :param transducer_amount_outputs: The amount of outputs that the transducer created in this block.
        :param new_transducer_state: The new transducer state of shape [2, 1, transducer_hidden_units]
        :return:
        """
        self.alignment_locations.append(index)
        self.alignment_position = (index, block_index)
        # TODO: look if new log_prob is done additively or absolute (I think additively)
        self.log_prob += self.__compute_sum_probabilities(transducer_outputs, targets, transducer_amount_outputs)
        self.last_state_transducer = new_transducer_state


# ----------------- Model -------------------------------
embeddings = tf.Variable(tf.random_uniform([vocab_size, input_embedding_size], -1.0, 1.0), dtype=tf.float32)


class Model(object):
    def __init__(self):
        self.max_blocks, self.inputs_full_raw, self.transducer_list_outputs, self.start_block, self.encoder_hidden_init,\
            self.trans_hidden_init, self.logits, self.encoder_hidden_state_new, \
            self.transducer_hidden_state_new, self.beam_search_outputs = self.build_full_transducer()

        self.targets, self.train_op, self.loss = self.build_training_step()

    def build_full_transducer(self):
        # Inputs
        max_blocks = tf.placeholder(dtype=tf.int32, name='max_blocks')  # total amount of blocks to go through
        inputs_full_raw = tf.placeholder(shape=(None, batch_size, input_dimensions), dtype=tf.float32,
                                         name='inputs_full_raw')  # shape [max_time, 1, input_dims]
        transducer_list_outputs = tf.placeholder(shape=(None,), dtype=tf.int32,
                                                 name='transducer_list_outputs')  # amount to output per block
        start_block = tf.placeholder(dtype=tf.int32, name='transducer_start_block')  # where to start the input

        encoder_hidden_init = tf.placeholder(shape=(2, 1, encoder_hidden_units), dtype=tf.float32,
                                             name='encoder_hidden_init')
        trans_hidden_init = tf.placeholder(shape=(2, 1, transducer_hidden_units), dtype=tf.float32,
                                           name='trans_hidden_init')
        inference = tf.placeholder(dtype=tf.int32, name='beam_width')  # Beam search inference, select 0 to disable

        # Turn inputs into tensor which is easily readable
        inputs_full = tf.reshape(inputs_full_raw, shape=[-1, input_block_size, batch_size, input_dimensions])

        # Outputs
        outputs_ta = tf.TensorArray(dtype=tf.float32, size=max_blocks)

        init_state = (start_block, outputs_ta, encoder_hidden_init, trans_hidden_init)

        def cond(current_block, outputs_int, encoder_hidden, trans_hidden):
            return current_block < start_block + max_blocks

        def body(current_block, outputs_int, encoder_hidden, trans_hidden):

            # --------------------- ENCODER -------------------------------------------------------------------------

            encoder_inputs = inputs_full[current_block - start_block]
            encoder_inputs_length = [tf.shape(encoder_inputs)[0]]
            encoder_hidden_state = encoder_hidden

            if inputs_embedded is True:
                encoder_inputs_embedded = encoder_inputs
            else:
                encoder_inputs_embedded = tf.nn.embedding_lookup(embeddings, encoder_inputs)

            # Build model
            encoder_cell = tf.contrib.rnn.LSTMCell(encoder_hidden_units)

            # Build previous state
            encoder_hidden_c, encoder_hidden_h = tf.split(encoder_hidden_state, num_or_size_splits=2, axis=0)
            encoder_hidden_c = tf.reshape(encoder_hidden_c, shape=[-1, encoder_hidden_units])
            encoder_hidden_h = tf.reshape(encoder_hidden_h, shape=[-1, encoder_hidden_units])
            encoder_hidden_state_t = LSTMStateTuple(encoder_hidden_c, encoder_hidden_h)

            #   encoder_outputs: [max_time, batch_size, num_units]
            encoder_outputs, encoder_hidden_state_new = tf.nn.dynamic_rnn(
                encoder_cell, encoder_inputs_embedded,
                sequence_length=encoder_inputs_length, time_major=True,
                dtype=tf.float32, initial_state=encoder_hidden_state_t)

            # Modify output of encoder_hidden_state_new so that it can be fed back in again without problems.
            encoder_hidden_state_new = tf.concat([encoder_hidden_state_new.c, encoder_hidden_state_new.h], axis=0)
            encoder_hidden_state_new = tf.reshape(encoder_hidden_state_new, shape=[2, -1, encoder_hidden_units])

            # --------------------- TRANSDUCER -----------------------------------------------------------------------
            # ----- Pre processing ------
            encoder_raw_outputs = encoder_outputs
            trans_hidden_state = trans_hidden  # Save/load the state as one tensor
            transducer_amount_outputs = transducer_list_outputs[current_block - start_block]
            attention_states = tf.transpose(encoder_raw_outputs,
                                            [1, 0, 2])  # attention_states: [batch_size, max_time, num_units]

            # Build previous state
            trans_hidden_c, trans_hidden_h = tf.split(trans_hidden_state, num_or_size_splits=2, axis=0)
            trans_hidden_c = tf.reshape(trans_hidden_c, shape=[-1, transducer_hidden_units])
            trans_hidden_h = tf.reshape(trans_hidden_h, shape=[-1, transducer_hidden_units])
            trans_hidden_state_t = LSTMStateTuple(trans_hidden_c, trans_hidden_h)

            # ----- Core ----------------
            cell = tf.contrib.rnn.LSTMCell(transducer_hidden_units)
            projection_layer = layers_core.Dense(vocab_size, use_bias=False)

            attention_states = tf.cond(inference > 0,
                                       lambda: tf.contrib.seq2seq.tile_batch(attention_states, multiplier=beam_width),
                                       lambda: attention_states)
            attention_mechanism = tf.contrib.seq2seq.LuongAttention(num_units=encoder_hidden_units,
                                                                    memory=attention_states)

            decoder_cell = tf.contrib.seq2seq.AttentionWrapper(cell,
                                                               attention_mechanism,
                                                               attention_layer_size=transducer_hidden_units)

            # ----- Training/Inference --
            def training_decoder():
                decoder_init_state_train = decoder_cell.zero_state(1, tf.float32).clone(cell_state=trans_hidden_state_t)
                helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
                    embedding=embeddings,
                    start_tokens=tf.tile([GO_SYMBOL], [batch_size]),
                    end_token=END_SYMBOL)
                return tf.contrib.seq2seq.BasicDecoder(decoder_cell,
                                                       helper, decoder_init_state_train,
                                                       output_layer=projection_layer)

            def inference_decoder():

                initial_state = tf.nn.rnn_cell.LSTMStateTuple(
                    tf.contrib.seq2seq.tile_batch(trans_hidden_c, multiplier=beam_width),
                    tf.contrib.seq2seq.tile_batch(trans_hidden_h, multiplier=beam_width))

                decoder_init_state_inf = decoder_cell.zero_state(dtype=tf.float32, batch_size=1*beam_width).\
                    clone(cell_state=initial_state)

                return tf.contrib.seq2seq.BeamSearchDecoder(decoder_cell, embeddings,
                                                            start_tokens=tf.tile([GO_SYMBOL], [batch_size]),
                                                            end_token=E_SYMBOL, initial_state=decoder_init_state_inf,
                                                            beam_width=beam_width, output_layer=projection_layer)

            # Build both decoders for future use
            inference_decoder = inference_decoder()
            training_decoder = training_decoder()
            train_outputs, train_transducer_hidden_state_new, _ = tf.contrib.seq2seq.dynamic_decode(training_decoder,
                                                                                        output_time_major=True,
                                                                                        maximum_iterations=transducer_amount_outputs)

            inf_outputs, inf_transducer_hidden_state_new, inf_seq_len = tf.contrib.seq2seq.dynamic_decode(inference_decoder,
                                                                                        output_time_major=True,
                                                                                        maximum_iterations=transducer_amount_outputs)

            # ----- Post Processing -----
            def train_post():
                decoder_prediction = train_outputs.sample_id  # For debugging

                # Modify output of transducer_hidden_state_new so that it can be fed back in again without problems.
                transducer_hidden_state_new = tf.concat(
                    [train_transducer_hidden_state_new[0].c, train_transducer_hidden_state_new[0].h],
                    axis=0)
                transducer_hidden_state_new = tf.reshape(transducer_hidden_state_new,
                                                         shape=[2, -1, transducer_hidden_units])
                return transducer_hidden_state_new

            def inf_post():
                # NOTE: for inference the body function in the loop can only be executed once! This is due to the beam
                # search approach for inference
                transducer_hidden_state_new = inf_transducer_hidden_state_new
                return transducer_hidden_state_new

            #  if in training, logits of shape [max_time,batch_size,vocab_size],
            # if in inference the the shape is [max_time, batch_size, vocab_size, beam_width]
            logits = tf.cond(inference > 0,
                             lambda: inf_outputs.beam_search_decoder_output.scores,
                             lambda: train_outputs.rnn_output)

            # Note the outputs
            outputs_int = outputs_int.write(current_block - start_block, logits)

            # Process the new transducer state
            transducer_hidden_state_new = tf.cond(inference > 0, inf_post, train_post)

            return current_block + 1, outputs_int, encoder_hidden_state_new, transducer_hidden_state_new

        _, outputs_final, encoder_hidden_state_new, transducer_hidden_state_new = \
            tf.while_loop(cond, body, init_state, parallel_iterations=1)

        # Process outputs
        outputs = outputs_final.concat()
        logits = tf.reshape(outputs, shape=(-1, 1, vocab_size))  # And now its [max_output_time, batch_size, vocab]

        # TODO: process beam search
        beam_search_outputs = outputs

        return max_blocks, inputs_full_raw, transducer_list_outputs, start_block, encoder_hidden_init, \
               trans_hidden_init, logits, encoder_hidden_state_new, transducer_hidden_state_new, beam_search_outputs

    def build_training_step(self):
        targets = tf.placeholder(shape=(None,), dtype=tf.int32, name='targets')
        targets_one_hot = tf.one_hot(targets, depth=vocab_size, dtype=tf.float32)

        targets_one_hot = tf.Print(targets_one_hot, [targets_one_hot], message='Targets: ', summarize=100)
        targets_one_hot = tf.Print(targets_one_hot, [self.logits], message='Logits: ', summarize=100)

        stepwise_cross_entropy = tf.nn.softmax_cross_entropy_with_logits(labels=targets_one_hot,
                                                                         logits=self.logits)
        loss = tf.reduce_mean(stepwise_cross_entropy)
        train_op = tf.train.AdamOptimizer().minimize(loss)
        return targets, train_op, loss


def softmax(x, axis=None):
    e_x = np.exp(x - np.max(x, axis=axis, keepdims=True))
    return e_x / np.sum(e_x, axis=axis, keepdims=True)


model = Model()


# ----------------- Alignment --------------------------

def get_alignment(session, inputs, targets, input_block_size, transducer_max_width):
    """
    Finds the alignment of the target sequence to the actual output.
    :param session: The current session.
    :param inputs: The complete inputs for the encoder of shape [max_time, 1, input_dimensions], note padding if needed
    :param targets: The target sequence of shape [time] where each enty is an index.
    :param input_block_size: The width of one encoder block.
    :param transducer_max_width: The max width of one transducer block.
    :return: Returns a list of indices where <e>'s need to be inserted into the target sequence. (see paper)
    """

    def run_new_block(session, full_inputs, previous_alignments, block_index, transducer_max_width, targets,
                      total_blocks, last_encoder_state):
        """
        Runs one block of the alignment process.
        :param session: The current TF session.
        :param full_inputs: The full inputs. Shape: [max_time, 1, input_dimensions]
        :param previous_alignments: List of alignment objects from previous block step.
        :param block_index: The index of the current new block.
        :param transducer_max_width: The max width of the transducer block.
        :param targets: The full target array of shape [time]
        :param total_blocks: The total amount of blocks.
        :param last_encoder_state: The encoder state of the previous step. Shape [2, 1, encoder_hidden_units]
        :return: new_alignments as list of Alignment objects,
        last_encoder_state_new in shape of [2, 1, encoder_hidden_units]
        """

        last_encoder_state_new = last_encoder_state  # fallback value

        def run_transducer(session, inputs_full, encoder_state, transducer_state, transducer_width):
            """
            Runs a transducer on one block of inputs for transducer_amount_outputs.
            :param session: Current session.
            :param inputs_full: The full inputs. Shape: [max_time, 1, input_dimensions]
            :param transducer_state: The last transducer state as [2, 1, transducer_hidden_units] tensor.
            :param transducer_width: The amount of outputs the transducer should produce.
            :return: transducer outputs [max_output_time, 1, vocab], transducer_state [2, 1, transducer_hidden_units],
            encoder_state [2, 1, encoder_hidden_units]
            """
            logits, trans_state, enc_state = session.run([model.logits, model.transducer_hidden_state_new,
                                                             model.encoder_hidden_state_new],
                                                 feed_dict={
                                                     model.inputs_full_raw: inputs_full,
                                                     model.max_blocks: 1,
                                                     model.transducer_list_outputs: [transducer_width],
                                                     model.start_block: block_index,
                                                     model.encoder_hidden_init: encoder_state,
                                                     model.trans_hidden_init: transducer_state,
                                                 })
            # apply softmax on the outputs
            trans_out = softmax(logits, axis=2)

            return trans_out, trans_state, enc_state

        # Look into every existing alignment
        new_alignments = []
        for i in range(len(previous_alignments)):
            alignment = previous_alignments[i]

            # Expand the alignment for each transducer width, only look at valid options
            targets_length = len(targets)
            min_index = alignment.alignment_position[0] + transducer_max_width + \
                        max(-transducer_max_width,
                            targets_length - ((total_blocks - block_index + 1) * transducer_max_width
                                              + alignment.alignment_position[0]))
            max_index = alignment.alignment_position[0] + transducer_max_width + min(0, targets_length - (
                    alignment.alignment_position[0] + transducer_max_width))

            # new_alignment_index's value is equal to the index of y~ for that computation
            for new_alignment_index in range(min_index, max_index + 1):  # +1 so that the max_index is also used
                # print 'Alignment index: ' + str(new_alignment_index)
                # Create new alignment
                new_alignment = copy.deepcopy(alignment)
                new_alignment_width = new_alignment_index - new_alignment.alignment_position[0]
                trans_out, trans_state, last_encoder_state_new = run_transducer(session=session,
                                                                                inputs_full=full_inputs,
                                                                                encoder_state=last_encoder_state,
                                                                                transducer_state=alignment.last_state_transducer,
                                                                                transducer_width=new_alignment_width)
                # last_encoder_state_new being set every time again -> not relevant

                new_alignment.insert_alignment(new_alignment_index, block_index, trans_out, targets,
                                               new_alignment_width, trans_state)
                new_alignments.append(new_alignment)

        # Delete all overlapping alignments, keeping the highest log prob
        for a in reversed(new_alignments):
            for o in new_alignments:
                if o is not a and a.alignment_position == o.alignment_position and o.log_prob > a.log_prob:
                    if a in new_alignments:
                        new_alignments.remove(a)

        return new_alignments, last_encoder_state_new

    # Manage variables
    amount_of_input_blocks = int(np.ceil(inputs.shape[0] / input_block_size))
    current_block_index = 1
    current_alignments = [Alignment()]
    last_encoder_state = np.zeros(shape=(2, 1, encoder_hidden_units))

    # Do assertions to check whether everything was correctly set up.
    assert inputs.shape[0] % input_block_size == 0, \
        'Input shape not corresponding to input block size (add padding or see if batch first).'
    assert inputs.shape[2] == input_dimensions, 'Input dimension [2] not corresponding to specified input dimension.'
    assert inputs.shape[1] == 1, 'Batch size needs to be one.'
    assert transducer_max_width * amount_of_input_blocks >= len(targets), 'transducer_max_width to small for targets'

    for block in range(current_block_index, amount_of_input_blocks + 1):
        # Run all blocks
        current_alignments, last_encoder_state = run_new_block(session=session, full_inputs=inputs,
                                                               previous_alignments=current_alignments,
                                                               block_index=block,
                                                               transducer_max_width=transducer_max_width,
                                                               targets=targets, total_blocks=amount_of_input_blocks,
                                                               last_encoder_state=last_encoder_state)

    # Check if we've found an alignment, it should be one
    assert len(current_alignments) == 1

    return current_alignments[0].alignment_locations

# ----------------- Training --------------------------


def apply_training_step(session, inputs, targets, input_block_size, transducer_max_width):
    """
    Applies a training step to the transducer model. This method can be called multiple times from e.g. a loop.
    :param session: The current session.
    :param inputs: The full inputs. Shape: [max_time, 1, input_dimensions]
    :param targets: The full targets. Shape: [time]. Each entry is an index.
    :param input_block_size: The block width for the inputs.
    :param transducer_max_width: The max width for the transducer. Not including the output symbol <e>
    :return: Loss of this training step.
    """

    # Get alignment and insert it into the targets
    alignment = get_alignment(session=session, inputs=inputs, targets=targets, input_block_size=input_block_size,
                              transducer_max_width=transducer_max_width)
    print alignment

    offset = 0
    for e in alignment:
        targets.insert(e+offset, E_SYMBOL)
        offset += 1

    # Calc length for each transducer block
    lengths = []
    alignment.insert(0, 0)  # This is so that the length calculation is done correctly
    for i in range(1, len(alignment)):
        lengths.append(alignment[i] - alignment[i-1] + 1)

    print lengths

    # Init values
    encoder_hidden_init = np.zeros(shape=(2, 1, encoder_hidden_units))
    trans_hidden_init = np.zeros(shape=(2, 1, transducer_hidden_units))

    # Run training step
    _, loss = sess.run([model.train_op, model.loss], feed_dict={
        model.max_blocks: len(lengths),
        model.inputs_full_raw: inputs,
        model.transducer_list_outputs: lengths,
        model.targets: targets,
        model.start_block: 0,
        model.encoder_hidden_init: encoder_hidden_init,
        model.trans_hidden_init: trans_hidden_init
    })

    return loss

# ----------------- Inference --------------------------

# TODO: change model to allow optional usage of beam search decoder
# TODO: add beam search with score based on log softmax addition
# TODO: select best one a the end

# ---------------------- Testing -----------------------------

testy_targets = np.asarray([1, 1, 1, 1, 1])


def test_new_block():
    def run_block(block_index, prev):
        na, _ = run_new_block(None, block_inputs=None, previous_alignments=prev, block_index=block_index,
                              transducer_max_width=3, targets=testy_targets, total_blocks=4, last_encoder_state=None)
        return na

    na = [Alignment()]
    for i in range(0, 3):
        na = run_block(i + 2, na)
        print 'Round ' + str(i + 1) + ' -----------'
        for a in na:
            print 'Alignment: ' + str(a.alignment_position)
            print a.log_prob
            print a.alignment_locations
        print ''


# Testing the alignment class
def test_alignment_class():
    testyAlignment = Alignment()
    testy_outputs = np.asarray([[[0.1, 0.7, 0.2]], [[0.2, 0.1, 0.7]]])
    testyAlignment.insert_alignment(2, 1, testy_outputs, testy_targets, 2, None)
    print 'Log prob for test 1: ' + str(testyAlignment.log_prob)  # Should be: -2.65926003693
    testy_outputs = np.asarray([[[0.2, 0.7, 0.1]], [[0.3, 0.1, 0.6]], [[0.2, 0.1, 0.7]]])
    testyAlignment.insert_alignment(5, 2, testy_outputs, testy_targets, 3, None)
    print 'Log prob for test 2: ' + str(testyAlignment.log_prob)  # Should be: -4.96184512993
    print testyAlignment.alignment_locations


def test_get_alignment(sess):
    testy_inputs = np.random.uniform(-1.0, 1.0, size=(12, 1, input_dimensions))
    print get_alignment(sess, inputs=testy_inputs, targets=testy_targets, input_block_size=input_block_size,
                        transducer_max_width=2)


# ---------------------- Management -----------------------------

init = tf.global_variables_initializer()

with tf.Session() as sess:
    sess.run(init)

    # test_get_alignment(sess)

    # Apply training step
    for i in range(0, 1):
        print apply_training_step(session=sess, inputs=np.ones(shape=(5 * input_block_size, 1, input_dimensions)),
                                  input_block_size=input_block_size, targets=[1, 1, 1, 1, 1, 1, 1], transducer_max_width=2)

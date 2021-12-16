from utils import *
import numpy as np
from math import sqrt
from evaluation import get_metric, write_predictions
import os
import time
import logging
import tensorflow as tf
from tensorflow.contrib import layers
import pickle


class MODEL(object):

    def __init__(self, opt, word_embedding, domain_embedding, word_dict):
        with tf.name_scope('parameters'):
            self.opt = opt
            self.w2v = word_embedding
            self.w2v_domain = domain_embedding
            self.word_id_mapping = word_dict
            self.Winit = tf.random_uniform_initializer(minval=-0.01, maxval=0.01, seed=0.05)

            info = ''
            for arg in vars(opt):
                info += ('>>> {0}: {1}\n'.format(arg, getattr(opt, arg)))

            if not os.path.exists(r'./log/{}'.format(self.opt.task)):
                os.makedirs(r'./log/{}'.format(self.opt.task))
            filename = r'./log/{}/{}.txt'.format(self.opt.task, time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime()))
            self.logger = logging.getLogger(filename)
            self.logger.setLevel(logging.DEBUG)

            sh = logging.StreamHandler()
            th = logging.FileHandler(filename, 'a')

            self.logger.addHandler(sh)
            self.logger.addHandler(th)

            self.logger.info('{:-^80}'.format('Parameters'))
            self.logger.info(info + '\n')

        with tf.name_scope('embeddings'):
            self.word_embedding = tf.Variable(self.w2v, dtype=tf.float32, name='word_embedding', trainable=False)
            self.domain_embedding = tf.Variable(self.w2v_domain, dtype=tf.float32, name='domain_embedding', trainable=False)

        with tf.name_scope('inputs'):
            self.x = tf.placeholder(tf.int32, [None, self.opt.max_sentence_len], name='x')
            self.aspect_y = tf.placeholder(tf.int32, [None, self.opt.max_sentence_len, self.opt.class_num], name='aspect_y')
            self.opinion_y = tf.placeholder(tf.int32, [None, self.opt.max_sentence_len, self.opt.class_num], name='opinion_y')
            self.sentiment_y = tf.placeholder(tf.int32, [None, self.opt.max_sentence_len, self.opt.class_num], name='sentiment_y')
            self.word_mask = tf.placeholder(tf.float32, [None, self.opt.max_sentence_len], name='word_mask')
            self.senti_mask = tf.placeholder(tf.float32, [None, self.opt.max_sentence_len], name='senti_mask')
            self.position = tf.placeholder(tf.float32, [None, self.opt.max_sentence_len, self.opt.max_sentence_len], name='position_att')
            self.keep_prob1 = tf.placeholder(tf.float32)
            self.keep_prob2 = tf.placeholder(tf.float32)
            self.is_training = tf.placeholder(tf.bool)
            self.drop_block1 = DropBlock2D(keep_prob=self.keep_prob2, block_size=3)
            self.drop_block2 = DropBlock2D(keep_prob=self.keep_prob2, block_size=3)
            self.drop_block3 = DropBlock2D(keep_prob=self.keep_prob2, block_size=3)


    def RACL(self, inputs, position_att):
        batch_size = tf.shape(inputs)[0]
        inputs = tf.nn.dropout(inputs, keep_prob=self.keep_prob1)

        # Shared Feature
        inputs = tf.layers.conv1d(inputs, self.opt.emb_dim, 1, padding='SAME', activation=tf.nn.relu, name='inputs')
        inputs = tf.nn.dropout(inputs, keep_prob=self.keep_prob1)

        mask256 = tf.tile(tf.expand_dims(self.word_mask, -1), [1, 1, self.opt.filter_num])
        mask70 = tf.tile(tf.expand_dims(self.word_mask, 1), [1, self.opt.max_sentence_len, 1])

        # Private Feature
        aspect_input, opinion_input, context_input = list(), list(), list()
        aspect_prob_list, opinion_prob_list, senti_prob_list = list(), list(), list()
        aspect_input.append(inputs)
        opinion_input.append(inputs)
        context_input.append(inputs)

        # We found that the SC task is more difficult than the AE and OE tasks.
        # Hence, we augment it with a memory-like mechanism by updating the aspect query with the retrieved contexts.
        # Refer to https://www.aclweb.org/anthology/D16-1021/ for for more details about the memory network.
        query = list()
        query.append(inputs)

        for hop in range(self.opt.hop_num):
            with tf.variable_scope('layers_{}'.format(hop)):
                # AE & OE Convolution
                aspect_conv = tf.layers.conv1d(aspect_input[-1], self.opt.filter_num, self.opt.kernel_size, padding='SAME', activation=tf.nn.relu, name='aspect_conv')
                opinion_conv = tf.layers.conv1d(opinion_input[-1], self.opt.filter_num, self.opt.kernel_size, padding='SAME', activation=tf.nn.relu, name='opinion_conv')

                # Relation R1
                aspect_see_opinion = tf.matmul(tf.nn.l2_normalize(aspect_conv, -1), tf.nn.l2_normalize(opinion_conv, -1), adjoint_b=True)
                aspect_att_opinion = softmask_2d(aspect_see_opinion, self.word_mask)
                aspect_inter = tf.concat([aspect_conv, tf.matmul(aspect_att_opinion, opinion_conv)], -1)

                opinion_see_aspect = tf.matmul(tf.nn.l2_normalize(opinion_conv, -1), tf.nn.l2_normalize(aspect_conv, -1), adjoint_b=True)
                opinion_att_aspect = softmask_2d(opinion_see_aspect, self.word_mask)
                opinion_inter = tf.concat([opinion_conv, tf.matmul(opinion_att_aspect, aspect_conv)], -1)

                # AE & OE Prediction
                aspect_p = layers.fully_connected(aspect_inter, self.opt.class_num, activation_fn=None, weights_initializer=self.Winit, biases_initializer=self.Winit, scope='aspect_p')
                opinion_p = layers.fully_connected(opinion_inter, self.opt.class_num, activation_fn=None, weights_initializer=self.Winit, biases_initializer=self.Winit, scope='opinion_p')

                # OE Confidence
                # A slight difference from the original paper.
                # For propagating R3, we calculate the confidence of each candidate opinion word.
                # Only when a word satisfies the condition Prob[B,I] > Prob[O] in OE, it can be propagated to SC.
                confidence = tf.maximum(0., 1 - 2. * tf.nn.softmax(opinion_p, -1)[:, :, 0])
                opinion_propagate = tf.tile(tf.expand_dims(confidence, 1), [1, self.opt.max_sentence_len, 1]) * mask70 * position_att

                # SC Convolution
                context_conv = tf.layers.conv1d(context_input[-1], self.opt.emb_dim, self.opt.kernel_size, padding='SAME', activation=tf.nn.relu, name='context_conv')

                # SC Aspect-Context Attention
                word_see_context = tf.matmul((query[-1]), tf.nn.l2_normalize(context_conv, -1), adjoint_b=True)  * position_att
                word_att_context = softmask_2d(word_see_context, self.word_mask, scale=True)

                # Relation R2 & R3
                word_att_context += aspect_att_opinion + opinion_propagate
                context_inter = (query[-1] + tf.matmul(word_att_context, context_conv)) # query + value
                query.append(context_inter) # update query

                # SC Prediction
                senti_p = layers.fully_connected(context_inter, self.opt.class_num, activation_fn=None, weights_initializer=self.Winit, biases_initializer=self.Winit, scope='senti_p')

                # Stacking
                aspect_prob_list.append(tf.expand_dims(aspect_p, -1))
                opinion_prob_list.append(tf.expand_dims(opinion_p, -1))
                senti_prob_list.append(tf.expand_dims(senti_p, -1))

                # We use DropBlock to enhance the learning of the private features for AE & OE & SC.
                # Refer to http://papers.nips.cc/paper/8271-dropblock-a-regularization-method-for-convolutional-networks for more details.
                aspect_inter = tf.squeeze(self.drop_block1(inputs=tf.expand_dims(aspect_inter, -1), training=self.is_training), -1)
                opinion_inter = tf.squeeze(self.drop_block2(inputs=tf.expand_dims(opinion_inter, -1), training=self.is_training), -1)
                context_conv = tf.squeeze(self.drop_block3(inputs=tf.expand_dims(context_conv, -1), training=self.is_training), -1)

                aspect_input.append(aspect_inter)
                opinion_input.append(opinion_inter)
                context_input.append(context_conv)

        # Multi-layer Short-cut
        aspect_prob = tf.reduce_mean(tf.concat(aspect_prob_list, -1), -1)
        opinion_prob = tf.reduce_mean(tf.concat(opinion_prob_list, -1), -1)
        sentiment_prob = tf.reduce_mean(tf.concat(senti_prob_list, -1), -1)

        return aspect_prob, opinion_prob, sentiment_prob

    def run(self):
        batch_size = tf.shape(self.x)[0]
        inputs_word = tf.nn.embedding_lookup(self.word_embedding, self.x)
        inputs_domain = tf.nn.embedding_lookup(self.domain_embedding, self.x)
        inputs = tf.concat([inputs_word, inputs_domain], -1)

        aspect_prob, opinion_prob, sentiment_prob = self.RACL(inputs, self.position)
        aspect_value = tf.nn.softmax(aspect_prob, -1)
        opinion_value = tf.nn.softmax(opinion_prob, -1)
        senti_value = tf.nn.softmax(sentiment_prob, -1)

        # AE & OE Regulation Loss
        reg_cost = tf.reduce_sum(tf.maximum(0., tf.reduce_sum(aspect_value[:,:,1:], -1) + tf.reduce_sum(opinion_value[:,:,1:], -1) - 1.)) / tf.reduce_sum(self.word_mask)

        # Mask AE & OE Probabilities
        word_mask = tf.tile(tf.expand_dims(self.word_mask, -1), [1, 1, self.opt.class_num])
        aspect_prob = tf.reshape(word_mask * aspect_prob, [-1, self.opt.class_num])
        aspect_label = tf.reshape(self.aspect_y, [-1, self.opt.class_num])
        opinion_prob = tf.reshape(word_mask * opinion_prob, [-1, self.opt.class_num])
        opinion_label = tf.reshape(self.opinion_y, [-1, self.opt.class_num])

        # Relation R4 (Only in Training)
        # In training/validation, the sentiment masks are set to 1.0 only for the aspect terms.
        # In testing, the sentiment masks are set to 1.0 for all words (except padding ones).
        senti_mask = tf.tile(tf.expand_dims(self.senti_mask, -1), [1, 1, self.opt.class_num])

        # Mask SC Probabilities
        sentiment_prob = tf.reshape(tf.cast(senti_mask, tf.float32) * sentiment_prob, [-1, self.opt.class_num])
        sentiment_label = tf.reshape(self.sentiment_y, [-1, self.opt.class_num])

        with tf.name_scope('loss'):
            tv = tf.trainable_variables()
            total_para = count_parameter()
            self.logger.info('>>> total parameter: {}'.format(total_para))

            aspect_cost = tf.reduce_mean(
                tf.nn.softmax_cross_entropy_with_logits(logits=aspect_prob, labels=tf.cast(aspect_label, tf.float32)))
            opinion_cost = tf.reduce_mean(
                tf.nn.softmax_cross_entropy_with_logits(logits=opinion_prob, labels=tf.cast(opinion_label, tf.float32)))
            sentiment_cost = tf.reduce_mean(
                tf.nn.softmax_cross_entropy_with_logits(logits=sentiment_prob, labels=tf.cast(sentiment_label, tf.float32)))

            cost = aspect_cost + opinion_cost + sentiment_cost + self.opt.reg_scale * reg_cost

        with tf.name_scope('train'):
            global_step = tf.Variable(0, name="tr_global_step", trainable=False)
            optimizer = tf.train.AdamOptimizer(learning_rate=self.opt.learning_rate).minimize(cost, global_step=global_step)

        with tf.name_scope('predict'):
            true_ay = tf.reshape(aspect_label, [batch_size, self.opt.max_sentence_len, -1])
            pred_ay = tf.reshape(aspect_prob, [batch_size, self.opt.max_sentence_len, -1])

            true_oy = tf.reshape(opinion_label, [batch_size, self.opt.max_sentence_len, -1])
            pred_oy = tf.reshape(opinion_prob, [batch_size, self.opt.max_sentence_len, -1])

            true_sy = tf.reshape(sentiment_label, [batch_size, self.opt.max_sentence_len, -1])
            pred_sy = tf.reshape(sentiment_prob, [batch_size, self.opt.max_sentence_len, -1])

        saver = tf.train.Saver(max_to_keep=10)
        # gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.2)
        with tf.Session() as sess:
            if self.opt.load == 0:
                init = tf.global_variables_initializer()
                sess.run(init)
            else:
                ckpt = tf.train.get_checkpoint_state('checkpoint/{}'.format(self.opt.task))
                saver.restore(sess, ckpt.model_checkpoint_path)

            train_sets = read_data(self.opt.train_path, self.word_id_mapping, self.opt.max_sentence_len)
            dev_sets = read_data(self.opt.dev_path, self.word_id_mapping, self.opt.max_sentence_len)
            test_sets = read_data(self.opt.test_path, self.word_id_mapping, self.opt.max_sentence_len, is_testing=True)

            aspect_f1_list = []
            opinion_f1_list = []
            sentiment_acc_list = []
            sentiment_f1_list = []
            ABSA_f1_list = []
            dev_metric_list = []
            dev_loss_list = []
            for i in range(self.opt.n_iter):
                'Train'
                tr_loss = 0.
                tr_aloss = 0.
                tr_oloss = 0.
                tr_sloss = 0.
                tr_rloss = 0.
                if self.opt.load == 0:
                    epoch_start = time.time()
                    for train, num in self.get_batch_data(train_sets, self.opt.batch_size, self.opt.kp1, self.opt.kp2, True, True):
                        tr_eloss, tr_aeloss, tr_oeloss, tr_seloss, tr_reloss, _, step = sess.run(
                        [cost, aspect_cost, opinion_cost, sentiment_cost, reg_cost, optimizer, global_step], feed_dict=train)
                        tr_loss += tr_eloss * num
                        tr_aloss += tr_aeloss * num
                        tr_oloss += tr_oeloss * num
                        tr_sloss += tr_seloss * num
                        tr_rloss += tr_reloss * num
                    #if i >= self.opt.warmup_iter:
                    #    saver.save(sess, 'checkpoint/{}/RACL.ckpt'.format(self.opt.task), global_step=i)
                    epoch_end = time.time()
                    epoch_time = 'Epoch Time: {:.0f}m {:.0f}s'.format((epoch_end - epoch_start) // 60, (epoch_end - epoch_start) % 60)

                'Test'
                a_preds, a_labels = [], []
                o_preds, o_labels = [], []
                s_preds, s_labels = [], []
                final_mask = []
                for test, _ in self.get_batch_data(test_sets, 200, 1.0, 1.0):
                    _step, t_ay, p_ay, t_oy, p_oy, t_sy, p_sy, e_mask = sess.run(
                        [global_step, true_ay, pred_ay, true_oy, pred_oy, true_sy, pred_sy, self.word_mask], feed_dict=test)
                    a_preds.extend(p_ay)
                    a_labels.extend(t_ay)
                    o_preds.extend(p_oy)
                    o_labels.extend(t_oy)
                    s_preds.extend(p_sy)
                    s_labels.extend(t_sy)
                    final_mask.extend(e_mask)

                aspect_f1, opinion_f1, sentiment_acc, sentiment_f1, ABSA_f1 \
                    = get_metric(a_labels, a_preds, o_labels, o_preds, s_labels, s_preds, final_mask, 1)

                aspect_f1_list.append(aspect_f1)
                opinion_f1_list.append(opinion_f1)
                sentiment_acc_list.append(sentiment_acc)
                sentiment_f1_list.append(sentiment_f1)
                ABSA_f1_list.append(ABSA_f1)

                #self.logger.info("target preds: {0}".format(a_preds))
                #self.logger.info("opinion preds: {0}".format(o_preds))
                #self.logger.info("opinion labels: {0}".format(o_labels))
                #self.logger.info("sent preds: {0}".format(s_preds))
                #self.logger.info("sent labels: {0}".format(s_labels))

                'Dev'
                dev_loss = 0.
                dev_aloss = 0.
                dev_oloss = 0.
                dev_sloss = 0.
                dev_rloss = 0.
                dev_a_preds, dev_a_labels = [], []
                dev_o_preds, dev_o_labels = [], []
                dev_s_preds, dev_s_labels = [], []
                dev_final_mask = []
                for dev, num in self.get_batch_data(dev_sets, 200, 1.0, 1.0):
                    dev_eloss, dev_aeloss, dev_oeloss, dev_seloss, dev_reloss, _step, dev_t_ay, dev_p_ay, dev_t_oy, dev_p_oy, dev_t_sy, dev_p_sy, dev_e_mask = \
                        sess.run([cost, aspect_cost, opinion_cost, sentiment_cost, reg_cost, global_step, true_ay, pred_ay, true_oy, pred_oy, true_sy, pred_sy, self.word_mask],
                                 feed_dict=dev)
                    dev_a_preds.extend(dev_p_ay)
                    dev_a_labels.extend(dev_t_ay)
                    dev_o_preds.extend(dev_p_oy)
                    dev_o_labels.extend(dev_t_oy)
                    dev_s_preds.extend(dev_p_sy)
                    dev_s_labels.extend(dev_t_sy)
                    dev_final_mask.extend(dev_e_mask)
                    dev_loss += dev_eloss * num
                    dev_aloss += dev_aeloss * num
                    dev_oloss += dev_oeloss * num
                    dev_sloss += dev_seloss * num
                    dev_rloss += dev_reloss * num

                dev_aspect_f1, dev_opinion_f1, dev_sentiment_acc, dev_sentiment_f1, dev_ABSA_f1 \
                    = get_metric(dev_a_labels, dev_a_preds, dev_o_labels, dev_o_preds, dev_s_labels, dev_s_preds,
                                 dev_final_mask, 1)

                if i < self.opt.warmup_iter:
                    dev_metric_list.append(0.)
                    dev_loss_list.append(1000.)
                else:
                    dev_metric_list.append(dev_ABSA_f1)
                    dev_loss_list.append(dev_loss)
                    if dev_ABSA_f1 > dev_metric_list[-2]:
                        self.logger.info("\nNew Best Dev: {0:.3f}".format(dev_ABSA_f1))
                        self.logger.info("\nSaving model...")
                        saver.save(sess, 'checkpoint/{0}/RACL-dev:{1}.ckpt'.format(self.opt.task, dev_ABSA_f1), global_step=i)


                if self.opt.load == 0:
                    self.logger.info('\n{:-^80}'.format('Iter' + str(i)))

                    self.logger.info('Train: final loss={:.6f}, aspect loss={:.6f}, opinion loss={:.6f}, sentiment loss={:.6f}, reg loss={:.6f}, step={}'.
                        format(tr_loss, tr_aloss, tr_oloss, tr_sloss, tr_rloss, step))
                    self.logger.info('Dev:   final loss={:.6f}, aspect loss={:.6f}, opinion loss={:.6f}, sentiment loss={:.6f}, reg loss={:.6f}, step={}'.
                        format(dev_loss, dev_aloss, dev_oloss, dev_sloss, dev_rloss, step))

                    self.logger.info('Dev:   aspect f1={:.4f}, opinion f1={:.4f}, sentiment acc=={:.4f}, sentiment f1=={:.4f}, ABSA f1=={:.4f},'
                        .format(dev_aspect_f1, dev_opinion_f1, dev_sentiment_acc, dev_sentiment_f1, dev_ABSA_f1))
                    self.logger.info('Test:  aspect f1={:.4f}, opinion f1={:.4f}, sentiment acc=={:.4f}, sentiment f1=={:.4f}, ABSA f1=={:.4f},'
                        .format(aspect_f1, opinion_f1, sentiment_acc, sentiment_f1, ABSA_f1))

                    self.logger.info('Current Max Metrics Index : {} Current Min Loss Index : {} {}'
                          .format(dev_metric_list.index(max(dev_metric_list)), dev_loss_list.index(min(dev_loss_list)), epoch_time))

                if self.opt.load == 1:
                    break
            self.logger.info('\n{:-^80}'.format('Mission Complete'))

            outdir = os.path.join("predictions", self.opt.task, "dev")
            os.makedirs(outdir, exist_ok=True)
            self.logger.info('\n{:-^80}'.format('Writing dev predictions to {}'.format(outdir)))
            write_predictions(dev_a_preds, dev_o_preds, dev_s_preds, dev_final_mask, outdir)

            outdir = os.path.join("predictions", self.opt.task, "test")
            os.makedirs(outdir, exist_ok=True)
            self.logger.info('\n{:-^80}'.format('Writing test predictions to {}'.format(outdir)))
            write_predictions(a_preds, o_preds, s_preds, final_mask, outdir)

            # max_dev_index = dev_metric_list.index(max(dev_metric_list))
            # self.logger.info('Dev Max Metrics Index: {}'.format(max_dev_index))
            # self.logger.info('aspect f1={:.4f}, opinion f1={:.4f}, sentiment acc=={:.4f}, sentiment f1=={:.4f}, ABSA f1=={:.4f},'
            #       .format(aspect_f1_list[max_dev_index], opinion_f1_list[max_dev_index],
            #               sentiment_acc_list[max_dev_index],
            #               sentiment_f1_list[max_dev_index], ABSA_f1_list[max_dev_index]))

            # min_dev_index = dev_loss_list.index(min(dev_loss_list))
            # self.logger.info('Dev Min Loss Index: {}'.format(min_dev_index))
            # self.logger.info('aspect f1={:.4f}, opinion f1={:.4f}, sentiment acc=={:.4f}, sentiment f1=={:.4f}, ABSA f1=={:.4f},'
            #       .format(aspect_f1_list[min_dev_index], opinion_f1_list[min_dev_index],
            #               sentiment_acc_list[min_dev_index],
            #               sentiment_f1_list[min_dev_index], ABSA_f1_list[min_dev_index]))

    def get_batch_data(self, dataset, batch_size, keep_prob1, keep_prob2, is_training=False, is_shuffle=False):
        length = len(dataset[0])
        all_index = np.arange(length)
        if is_shuffle:
            np.random.shuffle(all_index)
        for i in range(int(length / batch_size) + (1 if length % batch_size else 0)):
            index = all_index[i * batch_size:(i + 1) * batch_size]
            feed_dict = {
                self.x: dataset[0][index],
                self.aspect_y: dataset[1][index],
                self.opinion_y: dataset[2][index],
                self.sentiment_y: dataset[3][index],
                self.word_mask: dataset[4][index],
                self.senti_mask: dataset[5][index],
                self.position: dataset[6][index],
                self.keep_prob1: keep_prob1,
                self.keep_prob2: keep_prob2,
                self.is_training: is_training,
            }
            yield feed_dict, len(index)

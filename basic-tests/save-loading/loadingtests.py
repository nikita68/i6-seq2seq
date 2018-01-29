import tensorflow as tf
import numpy
rng = numpy.random
import os

dir = os.path.dirname(os.path.realpath(__file__))


with tf.variable_scope('old_scope'):
    x1 = tf.get_variable(name='x', shape=(), initializer=tf.zeros_initializer)
    y1 = tf.get_variable(name='y', shape=(), initializer=tf.zeros_initializer)

with tf.variable_scope('new_scope'):
    x2 = tf.get_variable(name='x', shape=(), initializer=tf.ones_initializer)
    y2 = tf.get_variable(name='y', shape=(), initializer=tf.ones_initializer)

saver = tf.train.Saver(var_list=tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='old_scope'))

init = tf.global_variables_initializer()

with tf.Session() as sess:
    sess.run(init)
    saver.save(sess, dir + '/data_1.ckpt')


loader = tf.train.Saver(var_list={'old_scope/x': x2, 'old_scope/y': y2})

with tf.Session() as sess2:
    loader.restore(sess2, dir + '/data_1.ckpt')
    print sess2.run([x2, y2])

# Copyright 2014 Matthieu Courbariaux

# This file is part of deep-learning-multipliers.

# deep-learning-multipliers is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# deep-learning-multipliers is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with deep-learning-multipliers.  If not, see <http://www.gnu.org/licenses/>.

import gzip
import cPickle
import numpy as np
import os
import os.path
import sys
import theano 
import theano.tensor as T
from theano import pp
import time
import scipy.stats
from pylearn2.sandbox.cuda_convnet.filter_acts import FilterActs
from theano.sandbox.cuda.basic_ops import gpu_contiguous
from pylearn2.sandbox.cuda_convnet.pool import MaxPool

from format import apply_format, overflow, new_range
        
class dropout_layer(object):
    
    def __init__(self, rng, p, scale, max_col_norm, format,
        comp_precision, update_precision, initial_range, max_overflow, w_LR_scale = 1., b_LR_scale = 1.):
        
        print "        p = " + str(p)
        print "        scale = " + str(scale)
        print "        w_LR_scale = " + str(w_LR_scale)
        print "        b_LR_scale = " + str(b_LR_scale)
        print "        max_col_norm = " + str(max_col_norm)
        print "        format = " + str(format)
        
        # save the parameters
        self.p = p
        self.scale = scale
        self.w_LR_scale = w_LR_scale
        self.b_LR_scale = b_LR_scale
        self.rng = rng
        self.max_col_norm = max_col_norm
        self.format = format
        
        # create shared variables
        self.comp_precision = theano.shared(value=comp_precision, name='comp_precision')
        self.update_precision = theano.shared(value=update_precision, name='update_precision')
        self.max_overflow = theano.shared(value=max_overflow, name='max_overflow')

        # create shared variables for the fixed point range
        self.z_range = theano.shared(value=initial_range, name='z_range')
        self.dEdz_range = theano.shared(value=initial_range, name='dEdz_range')
        self.y_range = theano.shared(value=initial_range, name='y_range')
        self.dEdy_range = theano.shared(value=initial_range, name='dEdy_range')
        self.w_range = theano.shared(value=initial_range, name='w_range')
        self.b_range = theano.shared(value=initial_range, name='b_range')
        self.dEdw_range = theano.shared(value=initial_range, name='dEdw_range')
        self.dEdb_range = theano.shared(value=initial_range, name='dEdb_range')
        self.update_w_range = theano.shared(value=initial_range, name='update_w_range')
        self.update_b_range = theano.shared(value=initial_range, name='update_b_range')
        
        # overflow counters for current range (needed to know when to augment the range)
        self.z_overflow = theano.shared(value=0., name='z_overflow')
        self.dEdz_overflow = theano.shared(value=0., name='dEdz_overflow')
        self.y_overflow = theano.shared(value=0., name='y_overflow')
        self.dEdy_overflow = theano.shared(value=0., name='dEdy_overflow')
        self.w_overflow = theano.shared(value=0., name='w_overflow')
        self.b_overflow = theano.shared(value=0., name='b_overflow')
        self.dEdw_overflow = theano.shared(value=0., name='dEdw_overflow')
        self.dEdb_overflow = theano.shared(value=0., name='dEdb_overflow')
        self.update_w_overflow = theano.shared(value=0., name='update_w_overflow')
        self.update_b_overflow = theano.shared(value=0., name='update_b_overflow')
    
        # overflow counter for current range-1 (needed to know when to reduce the range)
        self.z_overflow_1 = theano.shared(value=0., name='z_overflow_1')
        self.dEdz_overflow_1 = theano.shared(value=0., name='dEdz_overflow_1')
        self.y_overflow_1 = theano.shared(value=0., name='y_overflow_1')
        self.dEdy_overflow_1 = theano.shared(value=0., name='dEdy_overflow_1')
        self.w_overflow_1 = theano.shared(value=0., name='w_overflow_1')
        self.b_overflow_1 = theano.shared(value=0., name='b_overflow_1')
        self.dEdw_overflow_1 = theano.shared(value=0., name='dEdw_overflow_1')
        self.dEdb_overflow_1 = theano.shared(value=0., name='dEdb_overflow_1')
        self.update_w_overflow_1 = theano.shared(value=0., name='update_w_overflow_1')
        self.update_b_overflow_1 = theano.shared(value=0., name='update_b_overflow_1')
        
    def fprop(self, input):
        
        # we reduce the precision of parameters for the computations
        self.w_comp = apply_format(self.format, self.W, self.comp_precision, self.w_range)
        self.b_comp = apply_format(self.format, self.b, self.comp_precision, self.b_range)
        
        # scaled weighted sum
        self.z = apply_format(self.format, T.dot(input, self.w_comp * self.scale) + self.b_comp*self.scale, self.comp_precision, self.z_range)
        
        # activation
        self.y = apply_format(self.format, self.activation(self.z), self.comp_precision, self.y_range)
        
        # return the output
        return self.y
        
    def dropout_fprop(self, input):
        
        # we reduce the precision of parameters for the computations
        self.fixed_W = apply_format(self.format, self.W, self.comp_precision, self.w_range)
        self.fixed_b = apply_format(self.format, self.b, self.comp_precision, self.b_range)
            
        # create the dropout mask
        # The cast is important because
        # int * float32 = float64 which pulls things off the gpu
        srng = T.shared_randomstreams.RandomStreams(self.rng.randint(999999))
        self.mask = T.cast(srng.binomial(n=1, p=self.p, size=T.shape(input)), theano.config.floatX)
        
        # apply the mask
        self.fixed_x = input * self.mask
        
        # weighted sum
        self.z = T.dot(self.fixed_x, self.fixed_W) + self.fixed_b
        self.fixed_z = apply_format(self.format, self.z, self.comp_precision, self.z_range)
        
        # activation
        self.y = self.activation(self.fixed_z)
        self.fixed_y = apply_format(self.format, self.y, self.comp_precision, self.y_range)
        
        # return the output
        return  self.fixed_y
    
    def activation(self):
        
        raise NotImplementedError("Subclass must implement abstract method")
    
    def activation_bprop(self):
        
        raise NotImplementedError("Subclass must implement abstract method")
        
    def bprop(self, dEdy):
   
        self.fixed_dEdy = apply_format(self.format, dEdy, self.comp_precision, self.dEdy_range)
        
        # activation
        self.activation_bprop()
         
        # compute gradients of parameters
        self.fixed_dEdW = apply_format(self.format, T.grad(cost = None, wrt=[self.fixed_W], known_grads={self.z:self.fixed_dEdz})[0], self.comp_precision, self.dEdw_range)
        self.fixed_dEdb = apply_format(self.format, T.grad(cost = None, wrt=[self.fixed_b], known_grads={self.z:self.fixed_dEdz})[0], self.comp_precision, self.dEdb_range)
        
        # weighted sum
        dEdx = T.grad(cost = None, wrt=[self.fixed_x], known_grads={self.z:self.fixed_dEdz})[0]
        
        # apply mask
        dEdx = self.mask * dEdx
        
        return dEdx
        
    def parameter_updates(self, LR, M):    
        
        # compute updates
        new_update_W = apply_format(self.format, M * self.update_W - LR * self.w_LR_scale * self.fixed_dEdW, self.comp_precision, self.update_w_range)
        new_update_b = apply_format(self.format, M * self.update_b - LR * self.b_LR_scale * self.fixed_dEdb, self.comp_precision, self.update_b_range)
        
        # compute new parameters. Note that we use a better precision than the other operations
        new_W = apply_format(self.format, self.W + new_update_W, self.update_precision, self.w_range)
        new_b = apply_format(self.format, self.b + new_update_b, self.update_precision, self.b_range)
        
        # L2 column constraint on W
        col_norms = T.sqrt(T.sum(T.sqr(new_W), axis=0))
        # col_norms = T.max(new_W, axis=0)
        desired_norms = T.clip(col_norms, 0, self.max_col_norm) # clip = saturate below min and beyond max
        new_W = apply_format(self.format, new_W * (desired_norms / (1e-7 + col_norms)), self.update_precision, self.w_range)
        # for some reason, works better than 
        # new_W = new_W * (desired_norms / col_norms)
        # It may be a kind of regularization
        
        # return the updates of shared variables
        updates = []
        updates.append((self.W, new_W))
        updates.append((self.b, new_b))
        updates.append((self.update_W, new_update_W))
        updates.append((self.update_b, new_update_b)) 
        
        return updates

    def overflow_updates(self):
    
        updates = []
        
        # update overflow counters for the dynamic fixed point
        updates.append((self.z_overflow, self.z_overflow + overflow(self.fixed_z, self.comp_precision, self.z_range)))
        updates.append((self.dEdz_overflow, self.dEdz_overflow + overflow(self.fixed_dEdz, self.comp_precision, self.dEdz_range)))
        updates.append((self.y_overflow, self.y_overflow + overflow(self.fixed_y, self.comp_precision, self.y_range)))
        updates.append((self.dEdy_overflow, self.dEdy_overflow + overflow(self.fixed_dEdy, self.comp_precision, self.dEdy_range)))
        updates.append((self.w_overflow, self.w_overflow + overflow(self.W, self.update_precision, self.w_range)))
        updates.append((self.b_overflow, self.b_overflow + overflow(self.b, self.update_precision, self.b_range)))
        updates.append((self.dEdw_overflow, self.dEdw_overflow + overflow(self.fixed_dEdW, self.comp_precision, self.dEdw_range)))
        updates.append((self.dEdb_overflow, self.dEdb_overflow + overflow(self.fixed_dEdb, self.comp_precision, self.dEdb_range)))
        updates.append((self.update_w_overflow, self.update_w_overflow + overflow(self.update_W, self.comp_precision, self.update_w_range)))
        updates.append((self.update_b_overflow, self.update_b_overflow + overflow(self.update_b, self.comp_precision, self.update_b_range)))
        
        updates.append((self.z_overflow_1, self.z_overflow_1 + overflow(self.fixed_z, self.comp_precision, self.z_range-1)))
        updates.append((self.dEdz_overflow_1, self.dEdz_overflow_1 + overflow(self.fixed_dEdz, self.comp_precision, self.dEdz_range-1)))
        updates.append((self.y_overflow_1, self.y_overflow_1 + overflow(self.fixed_y, self.comp_precision, self.y_range-1)))
        updates.append((self.dEdy_overflow_1, self.dEdy_overflow_1 + overflow(self.fixed_dEdy, self.comp_precision, self.dEdy_range-1)))
        updates.append((self.w_overflow_1, self.w_overflow_1 + overflow(self.W, self.update_precision, self.w_range-1)))
        updates.append((self.b_overflow_1, self.b_overflow_1 + overflow(self.b, self.update_precision, self.b_range-1)))
        updates.append((self.dEdw_overflow_1, self.dEdw_overflow_1 + overflow(self.fixed_dEdW, self.comp_precision, self.dEdw_range-1)))
        updates.append((self.dEdb_overflow_1, self.dEdb_overflow_1 + overflow(self.fixed_dEdb, self.comp_precision, self.dEdb_range-1)))
        updates.append((self.update_w_overflow_1, self.update_w_overflow_1 + overflow(self.update_W, self.comp_precision, self.update_w_range-1)))
        updates.append((self.update_b_overflow_1, self.update_b_overflow_1 + overflow(self.update_b, self.comp_precision, self.update_b_range-1)))
        
        return updates
    
    def range_updates(self,batch_count):
        
        updates = []
        
        # update the ranges according to the overflow counters
        updates.append((self.z_range, self.z_range+new_range(self.z_overflow/batch_count,self.z_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.dEdz_range, self.dEdz_range+new_range(self.dEdz_overflow/batch_count, self.dEdz_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.y_range, self.y_range+new_range(self.y_overflow/batch_count, self.y_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.dEdy_range, self.dEdy_range+new_range(self.dEdy_overflow/batch_count, self.dEdy_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.w_range, self.w_range+new_range(self.w_overflow/batch_count, self.w_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.b_range, self.b_range+new_range(self.b_overflow/batch_count, self.b_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.dEdw_range, self.dEdw_range+new_range(self.dEdw_overflow/batch_count, self.dEdw_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.dEdb_range, self.dEdb_range+new_range(self.dEdb_overflow/batch_count, self.dEdb_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.update_w_range, self.update_w_range+new_range(self.update_w_overflow/batch_count, self.update_w_overflow_1/batch_count, self.max_overflow)))
        updates.append((self.update_b_range, self.update_b_range+new_range(self.update_b_overflow/batch_count, self.update_b_overflow_1/batch_count, self.max_overflow)))

        # reset the overflow counters
        updates.append((self.z_overflow, 0.))
        updates.append((self.dEdz_overflow, 0.))
        updates.append((self.y_overflow, 0.))
        updates.append((self.dEdy_overflow, 0.))
        updates.append((self.w_overflow, 0.))
        updates.append((self.b_overflow, 0.))
        updates.append((self.dEdw_overflow, 0.))
        updates.append((self.dEdb_overflow, 0.))
        updates.append((self.update_w_overflow, 0.))
        updates.append((self.update_b_overflow, 0.))        
        
        updates.append((self.z_overflow_1, 0.))
        updates.append((self.dEdz_overflow_1, 0.))
        updates.append((self.y_overflow_1, 0.))
        updates.append((self.dEdy_overflow_1, 0.))
        updates.append((self.w_overflow_1, 0.))
        updates.append((self.b_overflow_1, 0.))
        updates.append((self.dEdw_overflow_1, 0.))
        updates.append((self.dEdb_overflow_1, 0.))
        updates.append((self.update_w_overflow_1, 0.))
        updates.append((self.update_b_overflow_1, 0.))
        
        return updates
        
    def print_range(self):
        
        print '            z NOIB = %i' %(self.z_range.get_value())
        print '            y NOIB = %i' %(self.y_range.get_value())
        print '            w NOIB = %i' %(self.w_range.get_value())
        print '            b NOIB = %i' %(self.b_range.get_value())
        print '            dEdz NOIB = %i' %(self.dEdz_range.get_value())
        print '            dEdy NOIB = %i' %(self.dEdy_range.get_value())
        print '            dEdw NOIB = %i' %(self.dEdw_range.get_value())
        print '            dEdb NOIB = %i' %(self.dEdb_range.get_value())
        print '            update w NOIB = %i' %(self.update_w_range.get_value())
        print '            update b NOIB = %i' %(self.update_b_range.get_value())
        
class MaxoutLayer(dropout_layer):

    def __init__(self, rng, n_inputs, n_units, n_pieces, p, scale, max_col_norm, format, 
        comp_precision, update_precision, initial_range, max_overflow):
        
        self.n_pieces=n_pieces
        self.n_inputs = n_inputs
        self.n_units = n_units
        
        print "        n_pieces = " + str(n_pieces)
        print "        n_inputs = " + str(n_inputs)
        print "        n_units = " + str(n_units)
        
        # call mother class constructor
        dropout_layer.__init__(self, rng, p, scale, max_col_norm, format, 
            comp_precision, update_precision, initial_range, max_overflow)
    
        # initial values of parameters
        low=-np.sqrt(6. / (n_inputs + n_units*n_pieces))
        high=np.sqrt(6. / (n_inputs + n_units*n_pieces))
        W_values = np.asarray(self.rng.uniform(low=low,high=high,size=(n_inputs, n_units*n_pieces)),dtype=theano.config.floatX)
        b_values = np.zeros((n_units*n_pieces), dtype=theano.config.floatX)
            
        # creation of shared symbolic variables
        # shared variables are the state of the built function
        # in practice, we put them in the GPU memory
        self.W = theano.shared(value=W_values, name='W')
        self.b = theano.shared(value=b_values, name='b')
        
        # momentum
        self.update_W = theano.shared(value=np.zeros((n_inputs, n_units*n_pieces), dtype=theano.config.floatX), name='update_W')
        self.update_b = theano.shared(value=b_values, name='update_b')
    
    # activation function
    def activation(self,z):
        
        y = T.reshape(z,(T.shape(z)[0], self.n_units, self.n_pieces))
        
        # maxout
        y = T.max(y,axis=2)
        
        y = T.reshape(y,(T.shape(z)[0],self.n_units))

        return y
    
    def activation_bprop(self):
    
        self.fixed_dEdz = apply_format(self.format, 
            T.grad(cost = None, wrt=[self.fixed_z], known_grads={self.y:self.fixed_dEdy})[0], 
            self.comp_precision, self.dEdz_range)
        
class SoftmaxLayer(dropout_layer):
    
    def __init__(self, rng, n_inputs, n_units, p, scale, max_col_norm, format, 
        comp_precision, update_precision, initial_range, max_overflow):
        
        self.n_inputs = n_inputs
        self.n_units = n_units
        
        print "        n_inputs = " + str(n_inputs)
        print "        n_units = " + str(n_units)
        
        # call mother class constructor
        dropout_layer.__init__(self, rng, p, scale, max_col_norm, format, 
            comp_precision, update_precision, initial_range, max_overflow)
            
        # initial values of parameters
        W_values = np.zeros((n_inputs, n_units), dtype=theano.config.floatX)
        b_values = np.zeros(n_units, dtype=theano.config.floatX)
            
        # creation of shared symbolic variables
        self.W = theano.shared(value=W_values, name='W')
        self.b = theano.shared(value=b_values, name='b')
        
        # momentum
        self.update_W = theano.shared(value=W_values, name='update_W')
        self.update_b = theano.shared(value=b_values, name='update_b')
    
        # activation function
    def activation(self,z):
        
        return T.nnet.softmax(z)
        
    def activation_bprop(self):
        
        self.fixed_dEdz = apply_format(self.format, self.fixed_dEdy, 
            self.comp_precision, self.dEdz_range)
        
class Maxout_conv_layer(dropout_layer): 
    
    def __init__(self, rng, image_shape, zero_pad, output_shape, filter_shape, filter_stride, n_pieces, pool_shape, pool_stride, p, scale, max_col_norm, format,
            comp_precision, update_precision, initial_range, max_overflow, w_LR_scale=1., b_LR_scale=1., partial_sum = 1):
        
        # call mother class constructor
        dropout_layer.__init__(self, rng, p, scale, max_col_norm, format, comp_precision, update_precision, initial_range, max_overflow, w_LR_scale, b_LR_scale)
        
        print '        output_shape = ' +str(output_shape)
        print '        image_shape = ' +str(image_shape)
        
        # add n zero on both side of the input 
        # 0 <-> valid convolution, result is smaller
        # filter_size -1 <-> full convolution, result is bigger !
        # valid convolution makes more sense to me. I use it to reduce the size of feature maps without using max pool.
        print '        zero_pad = ' +str(zero_pad)
        
        # number of output feature maps, number of inputs feature maps, x, y
        # number of inputs feature maps is important for the weights
        print '        filter_shape = ' +str(filter_shape)
        print '        filter_stride = ' +str(filter_stride)
        print '        n_pieces = ' +str(n_pieces)
        print '        pool_shape = ' +str(pool_shape)
        print '        pool_stride = ' +str(pool_stride)
        print '        partial_sum = ' +str(partial_sum)
        
        # save the parameters
        self.output_shape = output_shape
        self.image_shape = image_shape
        self.zero_pad = zero_pad
        self.filter_shape = (filter_shape[0]*n_pieces,filter_shape[1],filter_shape[2],filter_shape[3])
        self.filter_stride = filter_stride
        self.n_pieces = n_pieces
        self.pool_shape = pool_shape  
        self.pool_stride = pool_stride 
        self.partial_sum = partial_sum 
        
        # range of init
        fan_in = np.prod(self.filter_shape[1:])
        fan_out = (self.filter_shape[0] * np.prod(self.filter_shape[2:]) /  self.n_pieces / np.prod(self.pool_shape)) 

        # initialize weights with random weights
        W_bound = np.sqrt(6. / (fan_in + fan_out))
        self.W = theano.shared(
            np.asarray(rng.uniform(low=-W_bound, high=W_bound, size=self.filter_shape),
                dtype=theano.config.floatX))
                
         # the bias is a 1D tensor -- one bias per output feature map
        b_values = np.zeros((self.filter_shape[0],), dtype=theano.config.floatX)
        self.b = theano.shared(value=b_values)
        
        self.update_W = theano.shared(value=np.zeros(self.filter_shape, dtype=theano.config.floatX), name='update_W')
        self.update_b = theano.shared(value=np.zeros((self.filter_shape[0],), dtype=theano.config.floatX), name='update_b')    

    # activation function
    def activation(self,conv_out):
        
        conv_out = T.reshape(conv_out,(T.shape(conv_out)[0], T.shape(conv_out)[1]//self.n_pieces, self.n_pieces,T.shape(conv_out)[2],T.shape(conv_out)[3] ))
        return T.max( conv_out,axis=2)
        
    def fprop(self, input):
        
        # we reduce the precision of parameters for the computations
        self.w_comp = apply_format(self.format, self.W, self.comp_precision, self.w_range)
        self.b_comp = apply_format(self.format, self.b, self.comp_precision, self.b_range)
        
        input = input.reshape(self.image_shape)
        
        # convolution
        input_shuffled = input.dimshuffle(1, 2, 3, 0) # bc01 to c01b
        filters_shuffled = self.w_comp.dimshuffle(1, 2, 3, 0) *self.scale # bc01 to c01b
        conv_op = FilterActs(stride=self.filter_stride, partial_sum=self.partial_sum,pad = self.zero_pad)
        contiguous_input = gpu_contiguous(input_shuffled)
        contiguous_filters = gpu_contiguous(filters_shuffled)
        conv_out_shuffled = conv_op(contiguous_input, contiguous_filters)
        
        # downsample each feature map individually, using maxpooling
        # pooled_out = downsample.max_pool_2d(input=conv_out,
        #                                     ds=poolsize, ignore_border=True)
        pool_op = MaxPool(ds=self.pool_shape, stride=self.pool_stride)
        pooled_out_shuffled = pool_op(conv_out_shuffled)
        pooled_out = pooled_out_shuffled.dimshuffle(3, 0, 1, 2) # c01b to bc01
        
        # bias
        pooled_out = apply_format(self.format, pooled_out + self.b_comp.dimshuffle('x', 0, 'x', 'x')*self.scale, self.comp_precision, self.z_range)
        
        # activation
        pooled_out = self.activation(pooled_out)
        pooled_out = apply_format(self.format, pooled_out.flatten(2), self.comp_precision, self.y_range)
        
        return pooled_out
    
    def dropout_fprop(self, input):
        
        # we reduce the precision of parameters for the computations
        self.fixed_W = apply_format(self.format, self.W, self.comp_precision, self.w_range)
        self.fixed_b = apply_format(self.format, self.b, self.comp_precision, self.b_range)
        
        # create the dropout mask
        # The cast is important because
        # int * float32 = float64 which pulls things off the gpu
        
        srng = T.shared_randomstreams.RandomStreams(self.rng.randint(999999))
        self.mask = T.cast(srng.binomial(n=1, p=self.p, size=T.shape(input)), theano.config.floatX)
        input = input * self.mask
        
        self.fixed_x = input.reshape(self.image_shape)

        # convolution
        input_shuffled = self.fixed_x.dimshuffle(1, 2, 3, 0) # bc01 to c01b
        filters_shuffled = self.fixed_W.dimshuffle(1, 2, 3, 0) # bc01 to c01b
        conv_op = FilterActs(stride=self.filter_stride, partial_sum=self.partial_sum,pad = self.zero_pad) # augment partial sum -> use less memory but slower
        contiguous_input = gpu_contiguous(input_shuffled)
        contiguous_filters = gpu_contiguous(filters_shuffled)
        conv_out_shuffled = conv_op(contiguous_input, contiguous_filters)
        
        self.z = conv_out_shuffled.dimshuffle(3, 0, 1, 2) # c01b to bc01
        self.fixed_z = apply_format(self.format, self.z, self.comp_precision, self.z_range) 
        
        conv_out_shuffled = self.fixed_z.dimshuffle(1, 2, 3, 0) # bc01 to c01b
        conv_out_shuffled = gpu_contiguous(conv_out_shuffled)
        
        # downsample each feature map individually, using maxpooling
        # pooled_out = downsample.max_pool_2d(input=conv_out,
        #                                     ds=poolsize, ignore_border=True)
        pool_op = MaxPool(ds=self.pool_shape, stride=self.pool_stride)
        pooled_out_shuffled = pool_op(conv_out_shuffled)
        pooled_out = pooled_out_shuffled.dimshuffle(3, 0, 1, 2) # c01b to bc01
        
        # bias
        self.u = pooled_out + self.fixed_b.dimshuffle('x', 0, 'x', 'x')
        self.fixed_u =  apply_format(self.format, self.u, self.comp_precision, self.z_range)
        
        # activation
        self.y = self.activation(self.fixed_u).flatten(2)
        self.fixed_y = apply_format(self.format, self.y, self.comp_precision, self.y_range)
        
        return self.fixed_y
        
    def bprop(self, dEdy):
        
        self.fixed_dEdy = apply_format(self.format, dEdy.reshape(self.output_shape), self.comp_precision, self.dEdy_range)
        
        fixed_dEdu = apply_format(self.format, T.grad(cost = None, wrt=[self.fixed_u], known_grads={self.y:self.fixed_dEdy})[0],  self.comp_precision,self.dEdz_range)
        
        self.fixed_dEdb = apply_format(self.format, T.grad(cost = None, wrt=[self.fixed_b], known_grads={self.u:fixed_dEdu})[0],  self.comp_precision,self.dEdb_range)
        
        self.fixed_dEdz = apply_format(self.format, T.grad(cost = None, wrt=[self.fixed_z], known_grads={self.u:fixed_dEdu})[0], self.comp_precision, self.dEdz_range)
        
        self.fixed_dEdW = apply_format(self.format, T.grad(cost = None, wrt=[self.fixed_W], known_grads={self.z:self.fixed_dEdz})[0],  self.comp_precision,self.dEdw_range)
        
        dEdx = T.grad(cost = None, wrt=[self.fixed_x], known_grads={self.z:self.fixed_dEdz})[0]
        
        dEdx = T.reshape(self.mask,T.shape(dEdx)) * dEdx
        
        return dEdx     
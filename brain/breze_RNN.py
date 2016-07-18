import math
import time
import os
import errno
from datetime import datetime
from data import toUTCtimestamp
import logging

import numpy as np

from data import load_multiple
import globals as st

#from breze.learn.data import interleave, padzeros, split
#from breze.learn import base
#from breze.learn.rnn import SupervisedFastDropoutRnn, SupervisedRnn
from breze.learn.rnn import SupervisedRnn
#import breze.learn.display as D
from breze.arch.component.loss import bern_ces

import climin.initialize
import climin.stops
import climin.mathadapt as ma
#from climin.initialize import bound_spectral_radius

import matplotlib.pyplot as plt

import csv
logging.basicConfig(filename='breze_RNN.log', level=logging.INFO)

def get_shaped_input(participant, series, subsample=0):
    '''

    receive and reshape (emg) data to:
    X = [300 x 2428 x 5]
    Z = [300 x 1320 x 2]

    IMPORTANT: Breze wants the data to be aligned (timesteps, samples, dimensionality)

    :param participant:
    :param series:
    :return:
    '''
    # data, eventNames = get_eeg_emg(participant, series, "emg")
    data, eventNames = load_multiple(participant, series, 'emg')
    p_train = 0.8
    p_val = 0.1
    n_train = int(len(data) * p_train)
    n_val = int(len(data) * (p_val + p_train)) - int(len(data) * p_train)
    n_test = len(data) - n_train - n_val

    print('[*] Samples Train: %d' %(n_train))
    print('[*] Samples Validation: %d' % (n_val))
    print('[*] Samples Test: %d' % (n_test))

    len_arr = [len(trial['emg_target']) for trial in data]
    max_seqlength = max(len_arr)
    min_seqlength = min(len_arr)
    print('[*] min seqlength: %i' % min_seqlength)
    print('[*] max seqlength: %i' % max_seqlength)

    seqlength = min_seqlength

    number_samples_train = 0;
    number_samples_val = 0;
    number_samples_test = 0;
    for trial_id in range(len(data)):
        if trial_id < n_train:
            number_samples_train += len(data[trial_id]['emg_target'])
        elif trial_id < n_train + n_val:
            number_samples_val +=  len(data[trial_id]['emg_target'])
        else:
            number_samples_test += len(data[trial_id]['emg_target'])


    X = np.zeros((number_samples_train, st.N_EMG_SENSORS))
    Z = np.zeros((number_samples_train, st.N_EMG_TARGETS))
    VX = np.zeros((number_samples_val, st.N_EMG_SENSORS))
    VZ = np.zeros((number_samples_val, st.N_EMG_TARGETS))
    TX = np.zeros((max_seqlength, n_test, st.N_EMG_SENSORS))
    TZ = np.zeros((max_seqlength, n_test, st.N_EMG_TARGETS))
    counter_train = 0
    counter_val = 0
    counter_test = 0
    for trial_id in range(len(data)):
        timestep = 0
        for sensor_set in data[trial_id]['emg_target'].iteritems():
            if trial_id < n_train:
                X[counter_train, ...] = sensor_set[1][0:st.N_EMG_SENSORS]
                Z[counter_train, ...] = sensor_set[1][st.N_EMG_SENSORS:st.N_EMG_SENSORS+st.N_EMG_TARGETS]
                counter_train +=1
            elif trial_id < n_train + n_val:
                VX[counter_val, ...] = sensor_set[1][0:st.N_EMG_SENSORS]
                VZ[counter_val, ...] = sensor_set[1][st.N_EMG_SENSORS:st.N_EMG_SENSORS + st.N_EMG_TARGETS]
                counter_val += 1
            else:
                TX[timestep, trial_id - n_train - n_val, ...] = sensor_set[1][0:st.N_EMG_SENSORS]
                TZ[timestep, trial_id - n_train - n_val, ...] = sensor_set[1][st.N_EMG_SENSORS:st.N_EMG_SENSORS + st.N_EMG_TARGETS]
            timestep += 1

    if subsample > 0:
        print('[*] Subsampling with factor %d' %(subsample))

        X = X[::subsample]
        Z = Z[::subsample]

        VX = VX[::subsample]
        VZ = VZ[::subsample]

        TX = TX[::subsample]
        TZ = TZ[::subsample]


        seqlength = seqlength/subsample
        seqlength_mod = seqlength % st.STRIDE_LEN
        seqlength -= seqlength_mod

    print('[*] Seqlenght: ' + str(seqlength))

    len_x_mod = len(X) % st.STRIDE_LEN
    len_x = len(X) - len_x_mod

    len_vx_mod = len(VX) % st.STRIDE_LEN
    len_vx = len(VX) - len_vx_mod

    # cut data to smallest overlap (along time axis)
    X_trim = X[:len_x]
    sX = X_trim.reshape(( -1,st.STRIDE_LEN, st.N_EMG_SENSORS)).transpose(1,0,2)

    Z_trim = Z[:len_x]
    sZ = Z_trim.reshape(( -1,st.STRIDE_LEN, st.N_EMG_TARGETS)).transpose(1,0,2)

    VX_trim = VX[:len_vx]
    sVX = VX_trim.reshape(( -1,st.STRIDE_LEN, st.N_EMG_SENSORS)).transpose(1,0,2)

    VZ_trim = VZ[:len_vx]
    sVZ = VZ_trim.reshape(( -1,st.STRIDE_LEN, st.N_EMG_TARGETS)).transpose(1,0,2)

    # No need to trim test set here
    # TX = TX[:seqlength]
    # TZ = TZ[:seqlength]

    print('[*] Shape Training Set X: ' + str(sX.shape))
    print('[*] Shape Training Set Z: ' + str(sZ.shape))
    print('[*] Shape Validation Set X: ' + str(sVX.shape))
    print('[*] Shape Validation Set Z: ' + str(sVZ.shape))
    print('[*] Shape Test Set X: ' + str(TX.shape))
    print('[*] Shape Test Set Z: ' + str(TZ.shape))

    return sX, sZ, sVX, sVZ, TX, TZ, seqlength, eventNames


def test_RNN(n_neurons=100, batch_size=50, participant=[1], series=[1, 2], subsample=10,
             imp_weights_skip=150, n_layers=1):
    format_string = '%d_%d_['
    for p in participant:
        format_string += '%d,' % p

    format_string = format_string[:-1]
    format_string += ']_['

    for s in series:
        format_string += '%d,' % s

    format_string = format_string[:-1]
    format_string += ']_%d_%d_%d_%d'
    net_info = format_string % (n_neurons, batch_size, subsample, imp_weights_skip, n_layers, time.time())
    logging.info('--------------------------------')
    logging.info('Rnn: n_neurons: %i, batch_size: %i, participant: %s, series: %s, subsample: %i, imp_weights_skip: %i'
                 % (n_neurons, batch_size, participant, series, subsample, imp_weights_skip))

    #optimizer = 'rmsprop', {'step_rate': 0.0001, 'momentum': 0.9, 'decay': 0.9}
    decay = 0.9
    offset = 1e-6
    mom = .9
    step_rate = .1
    optimizer = 'adadelta', {'decay': decay, 'offset': offset, 'momentum': mom, 'step_rate': step_rate}
    # optimizer = 'adam'
    n_hiddens = [n_neurons] * n_layers
    logging.info('optimizer: %s' % str(optimizer))

    m = SupervisedRnn(
        st.N_EMG_SENSORS, n_hiddens, st.N_EMG_TARGETS,  out_transfer='sigmoid', loss='bern_ces',
        hidden_transfers=['tanh'] * n_layers,
        batch_size=batch_size,
        imp_weight=True,
        optimizer=optimizer)

    sX, sZ, sVX, sVZ, TX, TZ, seqlength, eventNames = get_shaped_input(participant, series, subsample)

    W = np.ones_like(sZ)
    WV = np.ones_like(sVZ)
    #WT = np.ones_like(TX)
    W[:imp_weights_skip, :, :] = 0
    WV[:imp_weights_skip, :, :] = 0
    #WT[:imp_weights_skip, :, :] = 0


    m.exprs['true_loss'] = m.exprs['loss']
    # TODO: Test Loss: Doesn't work, don't know why, trying a hacky workaround in test_loss() - don't know if right this way
    # f_loss = m.function(['inpt', 'target', 'imp_weight'], 'true_loss')
    # print(f_loss([TX[:, :, :], TZ[:, :, :]]))
    # print(m.score(TX[:,0:1,:], TZ[:,0:1,:], WT[:,0:1,:]))  # similar error...

    # bern_ces changes the prediction!!! *facepalm*
    # ... just be careful to call it with a copy of the array
    def test_loss():
        return bern_ces(m.predict(TX)[:imp_weights_skip, :seqlength, :],
                        np.copy(TZ[:imp_weights_skip, :seqlength, :])).eval().mean()


    '''
    def test_nll():
        nll = 0
        n_time_steps = 0
        for x, z in zip(tx, tz):
            nll += f_loss(x[:, np.newaxis], z[:, np.newaxis]) * x.shape[0]
            n_time_steps += x.shape[0]
        return nll / n_time_steps
    '''


    climin.initialize.randomize_normal(m.parameters.data, 0, 0.1)
    #climin.initialize.bound_spectral_radius(m.parameters.data)

    #m.initialize(spectral_radius=1.2)

    def plot(test_sample=0, save_name='images/%s_test.png' % net_info, test_loss=None):
        colors = ['blue', 'red', 'green', 'cyan', 'magenta', 'orange', 'yellow']
        figure, (axes) = plt.subplots(3, 1)


        #input_for_plot = sVX.transpose(1,0,2).reshape((-1, seqlength, st.N_EMG_SENSORS)).transpose(1,0,2)[:, 0:1, :]
        #target_for_plot = sVZ.transpose(1,0,2).reshape((-1, seqlength, st.N_EMG_TARGETS)).transpose(1,0,2)[:, 0:1, :]

        input_for_plot = TX[:, test_sample:test_sample+1, :]
        # to be able to plot the test samples in correct length we have to determine where the '0' padding starts
        sample_length = min(np.append(len(input_for_plot), np.where(input_for_plot == np.zeros((st.N_EMG_TARGETS)))[0]))
        #sample_length = min(np.where(input_for_plot == np.zeros((st.N_EMG_TARGETS))[0]))
        input_for_plot = input_for_plot[:sample_length]
        target_for_plot = TZ[:sample_length, test_sample:test_sample+1, :]
        result = m.predict(input_for_plot)

        x_axis = np.arange(input_for_plot.shape[0])

        for i in range(st.N_EMG_TARGETS):

            axes[0].set_title('TARGETS')
            axes[0].fill_between(x_axis, 0, target_for_plot[:, 0, i], facecolor=colors[i], alpha=0.8,
                                 label=eventNames[st.SEQ_EMG_TARGETS.index(i)])
            #axes[0].plot(x_axis, target_for_plot[:, 0, i])
            if test_loss:
                axes[1].set_title('RNN (overall test loss: %f)' %(test_loss))
            else:
                axes[1].set_title('RNN')
            axes[1].plot(x_axis, result[:, 0, i], color=colors[i])

        train_loss = []
        val_loss = []
        test_loss = []

        for i in infos:
            train_loss.append(i['loss'])
            val_loss.append(i['val_loss'])
            test_loss.append(i['test_loss'])

        axes[2].set_title('LOSSES')
        axes[2].plot(np.arange(len(infos)), train_loss, label='train loss')
        axes[2].plot(np.arange(len(infos)), val_loss, label='validation loss')
        axes[2].plot(np.arange(len(infos)), test_loss, label='test loss')

        axes[0].legend(loc=0, shadow=True, fontsize='x-small')  # loc: 0=best, 1=upper right, 2=upper left

        axes[2].legend(loc=0, shadow=True, fontsize='x-small')

        figure.subplots_adjust(hspace=0.5)
        figure.savefig(save_name)
        plt.close(figure)


    max_passes = 200
    max_minutes = 10
    max_iter = max_passes * sX.shape[1] / m.batch_size
    batches_per_pass = int(math.ceil(float(sX.shape[1]) / m.batch_size))
    pause = climin.stops.ModuloNIterations(batches_per_pass * 1)  # after each pass through all data

    stop = climin.stops.Any([
        climin.stops.TimeElapsed(max_minutes * 60),  # maximal time in seconds
        climin.stops.AfterNIterations(max_iter)  # maximal iterations
        #climin.stops.Patience('val_loss', batches_per_pass*50, grow_factor=2.0, threshold=0.00001),  # kind of early stopping
        # climin.stops.NotBetterThanAfter(30, 100),  # error under 30 after 100 iterations?
    ])

    start = time.time()
    header = '#', 'seconds', 'loss', 'val loss', 'test loss'
    print '\t'.join(header)
    logging.info('\t'.join(header))


    infos = []
    try:
        os.makedirs('losses')
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir('losses'):
            pass
        else:
            raise

    f = open('losses/%s.csv' % net_info, 'wt')
    try:
        writer = csv.writer(f)
        writer.writerow(('Train loss', 'Validation loss', 'Test Loss'))
        for i, info in enumerate(m.powerfit((sX, sZ, W), (sVX, sVZ, WV), stop=stop,
                                            report=pause, eval_train_loss=True)):

            info['loss'] = float(info['loss'])
            info['val_loss'] = float(info['val_loss'])
            info['test_loss'] = float(ma.scalar(test_loss()))

            writer.writerow((info['loss'], info['val_loss'], info['test_loss']))

            info.update({
                'time': time.time() - start,
                # 'spectral_radius': get_spectral_radius(m.parameters['recurrent_0']),
            })
            template = '\t'.join(
                ['%(n_iter)i', '%(time)g', '%(loss)g', '%(val_loss)g', '%(test_loss)g'])
            row = template % info
            print row
            logging.info(row)
            filtered_info = dict(
                (k, v) for k, v in info.items()
                # if (not isinstance(v, (np.ndarray, gp.garray)) or v.size <= 1) and k not in ('args', 'kwargs'))
                if (not isinstance(v, (np.ndarray,)) or v.size <= 1) and k not in ('args', 'kwargs'))

            for key in filtered_info:
                if isinstance(filtered_info[key], np.float32):
                    filtered_info[key] = float(filtered_info[key])
            infos.append(filtered_info)


    finally:
        f.close()

    m.parameters.data[...] = info['best_pars']

    save_timestmp = toUTCtimestamp(datetime.utcnow())
    dot_idx = str(save_timestmp).index('.')
    save_timestmp = str(save_timestmp)[:dot_idx]

    logging.info('saved at: %s' % save_timestmp)

    plot(0, 'images/%s_emg_test0.png' % net_info, test_loss())
    plot(1, 'images/%s_emg_test1.png' % net_info, test_loss())
    plot(2, 'images/%s_emg_test2.png' % net_info, test_loss())
    plot(3, 'images/%s_emg_test3.png' % net_info, test_loss())

# Copyright (c) 2019, NVIDIA Corporation. All rights reserved.
#
# This work is made available under the Nvidia Source Code License-NC.
# To view a copy of this license, visit
# https://nvlabs.github.io/stylegan2/license.html

from collections import deque
import re
import struct
import sys
import time

import click
import jack
import numpy as np
from PIL import Image, ImageTk
from scipy import signal
import tkinter as tk

import dnnlib
import dnnlib.tflib as tflib
import pretrained_networks


def unpack_bytes(byte_string, int_count):
    """Unpacks a byte string to 32 bit little endian integers."""
    return struct.unpack(f'<{int_count}l', byte_string)


def aggressive_array_split(array, parts):
    # Potentially exclude some indeces to get equal bins
    end_index = len(array) - (len(array) % parts)
    return np.split(array[:end_index], parts)


def welch_periodogram(samples, sample_rate, bin_count):
    segment_size = int(samples.size / bin_count)
    _, spectral_density = signal.welch(
        samples,
        sample_rate,
        nperseg=segment_size,
        return_onesided=True
    )
    return spectral_density


def generate_periodogram_from_audio(audio_buffer, samples_per_frame, sample_rate, bin_count):
    while True:
        if len(audio_buffer) < samples_per_frame:
            time.sleep(0.001)
            continue

        audio =  np.array([audio_buffer.pop() for _ in range(samples_per_frame)])
        audio_mono = np.sum(audio, axis=1)
        print(f'{len(audio_buffer)} samples left in buffer')

        periodogram = welch_periodogram(audio_mono, sample_rate, bin_count)
        print(f'Raw periodogram shape: {periodogram.shape}')
        periodogram_split = aggressive_array_split(periodogram, bin_count)
        print(f'Split periodogram length: {len(periodogram_split)}')
        periodogram_summed = np.sum(periodogram_split, axis=1)
        print(f'Summed periodogram shape: {periodogram_summed.shape}')
        assert periodogram_summed.shape == (bin_count,)

        yield  periodogram_summed


def generate_images(network_pkl, seeds, truncation_psi, periodogram_generator, minibatch_size=4):
    print('Loading networks from "%s"...' % network_pkl)
    _G, _D, Gs = pretrained_networks.load_networks(network_pkl)
    average_weights = Gs.get_var('dlatent_avg') # [component]
    print(f'Average weights shape: {average_weights.shape}')

    Gs_syn_kwargs = dnnlib.EasyDict()
    Gs_syn_kwargs.output_transform = dict(func=tflib.convert_images_to_uint8, nchw_to_nhwc=True)
    Gs_syn_kwargs.randomize_noise = False
    Gs_syn_kwargs.minibatch_size = minibatch_size

    all_input_noise = np.stack([np.random.RandomState(seed).randn(*Gs.input_shape[1:]) for seed in seeds]) # [minibatch, component]
    
    for weights in periodogram_generator:
        print(f'Weights: {weights}')
        weighted_noise = weights.reshape(len(all_input_noise), 1) * all_input_noise
        weighted_sum = np.sum(weighted_noise, axis=0)
        normalised_noise = weighted_sum / np.linalg.norm(weighted_sum, ord=2, keepdims=True)
        normalised_noise = np.array([normalised_noise])

        layers = Gs.components.mapping.run(normalised_noise, None) # [minibatch, layer, component]
        layers = average_weights + (layers - average_weights) * truncation_psi # [minibatch, layer, component]
        images = Gs.components.synthesis.run(layers, **Gs_syn_kwargs) # [minibatch, height, width, channel]
        yield images[0]


@click.command()
@click.argument('jack_output_name')
@click.argument('network_pkl')
@click.option(
    '-s', 
    '--seeds', 
    type=str, 
    help='Comma-separated list of network input seeds. (Low-frequency to high-frequency.)'
)
@click.option('-p', '--truncation-psi', default=0.75)
@click.option('--samples-per-frame', default=2048)
@click.option('--sample-rate', default=48000)
def visualise(jack_output_name, network_pkl, seeds, truncation_psi, samples_per_frame, sample_rate):
    seeds = [int(seed.strip()) for seed in seeds.split(',') if seed]

    client = jack.Client('StyleGan Visualiser')
    input_one = client.inports.register('in_1')
    input_two = client.inports.register('in_2')
    external_output_one = client.get_port_by_name(f'{jack_output_name}:out_1')
    external_output_two = client.get_port_by_name(f'{jack_output_name}:out_2')

    raw_audio = deque()
    @client.set_process_callback
    def process(frame_count):
        nonlocal raw_audio

        buffer_one = unpack_bytes(
            input_one.get_buffer()[:], frame_count
        )
        buffer_two = unpack_bytes(
            input_two.get_buffer()[:], frame_count
        )
 
        for sample_one, sample_two in zip(buffer_one, buffer_two):
            raw_audio.appendleft((sample_one, sample_two))

    root = tk.Tk()
    panel = tk.Label(root)
    panel.pack(side='bottom', fill='both', expand='yes')

    periodogram_gen = generate_periodogram_from_audio(
        raw_audio,
        samples_per_frame,
        sample_rate,
        len(seeds)
    )
    with client:
        client.connect(external_output_one, input_one)
        client.connect(external_output_one, input_two)

        for image_array in generate_images(network_pkl, seeds, truncation_psi, periodogram_gen):
            image = Image.fromarray(image_array, 'RGB')
            gui_image = ImageTk.PhotoImage(image)

            panel.configure(image=gui_image)

            # To prevent GC getting rid of image?
            panel.image = gui_image

            root.update()


if __name__ == '__main__':
    visualise()


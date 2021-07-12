# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from omegaconf import MISSING, ListConfig, OmegaConf
from torch.nn.modules.conv import Conv1d

from nemo.collections.asr.parts.submodules.jasper import JasperBlock, MaskedConv1d, init_weights, jasper_activations
from nemo.collections.asr.parts.submodules.tdnn_attention import (
    AttentivePoolLayer,
    SE_TDNN_Module,
    StatsPoolLayer,
    TDNN_Module,
)
from nemo.core.classes.common import typecheck
from nemo.core.classes.exportable import Exportable
from nemo.core.classes.module import NeuralModule
from nemo.core.neural_types import (
    AcousticEncodedRepresentation,
    LengthsType,
    LogitsType,
    LogprobsType,
    NeuralType,
    SpectrogramType,
)
from nemo.utils import logging

__all__ = ['ConvASRDecoder', 'ConvASREncoder', 'ConvASRDecoderClassification']


class ConvASREncoder(NeuralModule, Exportable):
    """
    Convolutional encoder for ASR models. With this class you can implement JasperNet and QuartzNet models.

    Based on these papers:
        https://arxiv.org/pdf/1904.03288.pdf
        https://arxiv.org/pdf/1910.10261.pdf
    """

    def _prepare_for_export(self, **kwargs):
        m_count = 0
        for m in self.modules():
            if isinstance(m, MaskedConv1d):
                m.use_mask = False
                m_count += 1
        Exportable._prepare_for_export(self, **kwargs)
        logging.warning(f"Turned off {m_count} masked convolutions")

    def input_example(self):
        """
        Generates input examples for tracing etc.
        Returns:
            A tuple of input examples.
        """
        input_example = torch.randn(16, self._feat_in, 256).to(next(self.parameters()).device)
        return tuple([input_example])

    @property
    def disabled_deployment_input_names(self):
        """Implement this method to return a set of input names disabled for export"""
        return set(["length"])

    @property
    def disabled_deployment_output_names(self):
        """Implement this method to return a set of output names disabled for export"""
        return set(["encoded_lengths"])

    def save_to(self, save_path: str):
        pass

    @classmethod
    def restore_from(cls, restore_path: str):
        pass

    @property
    def input_types(self):
        """Returns definitions of module input ports.
        """
        return OrderedDict(
            {
                "audio_signal": NeuralType(('B', 'D', 'T'), SpectrogramType()),
                "length": NeuralType(tuple('B'), LengthsType()),
            }
        )

    @property
    def output_types(self):
        """Returns definitions of module output ports.
        """
        return OrderedDict(
            {
                "outputs": NeuralType(('B', 'D', 'T'), AcousticEncodedRepresentation()),
                "encoded_lengths": NeuralType(tuple('B'), LengthsType()),
            }
        )

    def __init__(
        self,
        jasper,
        activation: str,
        feat_in: int,
        normalization_mode: str = "batch",
        residual_mode: str = "add",
        norm_groups: int = -1,
        conv_mask: bool = True,
        frame_splicing: int = 1,
        init_mode: Optional[str] = 'xavier_uniform',
        quantize: bool = False,
    ):
        super().__init__()
        if isinstance(jasper, ListConfig):
            jasper = OmegaConf.to_container(jasper)

        activation = jasper_activations[activation]()

        # If the activation can be executed in place, do so.
        if hasattr(activation, 'inplace'):
            activation.inplace = True

        feat_in = feat_in * frame_splicing

        self._feat_in = feat_in

        residual_panes = []
        encoder_layers = []
        self.dense_residual = False
        for lcfg in jasper:
            dense_res = []
            if lcfg.get('residual_dense', False):
                residual_panes.append(feat_in)
                dense_res = residual_panes
                self.dense_residual = True
            groups = lcfg.get('groups', 1)
            separable = lcfg.get('separable', False)
            heads = lcfg.get('heads', -1)
            residual_mode = lcfg.get('residual_mode', residual_mode)
            se = lcfg.get('se', False)
            se_reduction_ratio = lcfg.get('se_reduction_ratio', 8)
            se_context_window = lcfg.get('se_context_size', -1)
            se_interpolation_mode = lcfg.get('se_interpolation_mode', 'nearest')
            kernel_size_factor = lcfg.get('kernel_size_factor', 1.0)
            stride_last = lcfg.get('stride_last', False)
            future_context = lcfg.get('future_context', -1)
            encoder_layers.append(
                JasperBlock(
                    feat_in,
                    lcfg['filters'],
                    repeat=lcfg['repeat'],
                    kernel_size=lcfg['kernel'],
                    stride=lcfg['stride'],
                    dilation=lcfg['dilation'],
                    dropout=lcfg['dropout'],
                    residual=lcfg['residual'],
                    groups=groups,
                    separable=separable,
                    heads=heads,
                    residual_mode=residual_mode,
                    normalization=normalization_mode,
                    norm_groups=norm_groups,
                    activation=activation,
                    residual_panes=dense_res,
                    conv_mask=conv_mask,
                    se=se,
                    se_reduction_ratio=se_reduction_ratio,
                    se_context_window=se_context_window,
                    se_interpolation_mode=se_interpolation_mode,
                    kernel_size_factor=kernel_size_factor,
                    stride_last=stride_last,
                    future_context=future_context,
                    quantize=quantize,
                )
            )
            feat_in = lcfg['filters']

        self._feat_out = feat_in

        self.encoder = torch.nn.Sequential(*encoder_layers)
        self.apply(lambda x: init_weights(x, mode=init_mode))

    @typecheck()
    def forward(self, audio_signal, length=None):
        s_input, length = self.encoder(([audio_signal], length))
        if length is None:
            return s_input[-1]

        return s_input[-1], length


class ConvASRDecoder(NeuralModule, Exportable):
    """Simple ASR Decoder for use with CTC-based models such as JasperNet and QuartzNet

     Based on these papers:
        https://arxiv.org/pdf/1904.03288.pdf
        https://arxiv.org/pdf/1910.10261.pdf
        https://arxiv.org/pdf/2005.04290.pdf
    """

    def save_to(self, save_path: str):
        pass

    @classmethod
    def restore_from(cls, restore_path: str):
        pass

    @property
    def input_types(self):
        return OrderedDict({"encoder_output": NeuralType(('B', 'D', 'T'), AcousticEncodedRepresentation())})

    @property
    def output_types(self):
        return OrderedDict({"logprobs": NeuralType(('B', 'T', 'D'), LogprobsType())})

    def __init__(self, feat_in, num_classes, init_mode="xavier_uniform", vocabulary=None):
        super().__init__()

        if vocabulary is not None:
            if num_classes != len(vocabulary):
                raise ValueError(
                    f"If vocabulary is specified, it's length should be equal to the num_classes. Instead got: num_classes={num_classes} and len(vocabulary)={len(vocabulary)}"
                )
            self.__vocabulary = vocabulary
        self._feat_in = feat_in
        # Add 1 for blank char
        self._num_classes = num_classes + 1

        self.decoder_layers = torch.nn.Sequential(
            torch.nn.Conv1d(self._feat_in, self._num_classes, kernel_size=1, bias=True)
        )
        self.apply(lambda x: init_weights(x, mode=init_mode))

    @typecheck()
    def forward(self, encoder_output):
        return torch.nn.functional.log_softmax(self.decoder_layers(encoder_output).transpose(1, 2), dim=-1)

    def input_example(self):
        """
        Generates input examples for tracing etc.
        Returns:
            A tuple of input examples.
        """
        bs = 8
        seq = 64
        input_example = torch.randn(bs, self._feat_in, seq).to(next(self.parameters()).device)
        return tuple([input_example])

    def _prepare_for_export(self, **kwargs):
        m_count = 0
        for m in self.modules():
            if type(m).__name__ == "MaskedConv1d":
                m.use_mask = False
                m_count += 1
        if m_count > 0:
            logging.warning(f"Turned off {m_count} masked convolutions")
        Exportable._prepare_for_export(self, **kwargs)

    @property
    def vocabulary(self):
        return self.__vocabulary

    @property
    def num_classes_with_blank(self):
        return self._num_classes


class ConvASRDecoderClassification(NeuralModule, Exportable):
    """Simple ASR Decoder for use with classification models such as JasperNet and QuartzNet

     Based on these papers:
        https://arxiv.org/pdf/2005.04290.pdf
    """

    def input_example(self):
        """
        Generates input examples for tracing etc.
        Returns:
            A tuple of input examples.
        """
        input_example = torch.randn(16, self._feat_in, 128).to(next(self.parameters()).device)
        return tuple([input_example])

    @property
    def input_types(self):
        return OrderedDict({"encoder_output": NeuralType(('B', 'D', 'T'), AcousticEncodedRepresentation())})

    @property
    def output_types(self):
        return OrderedDict({"logits": NeuralType(('B', 'D'), LogitsType())})

    def __init__(
        self,
        feat_in: int,
        num_classes: int,
        init_mode: Optional[str] = "xavier_uniform",
        return_logits: bool = True,
        pooling_type='avg',
    ):
        super().__init__()

        self._feat_in = feat_in
        self._return_logits = return_logits
        self._num_classes = num_classes

        if pooling_type == 'avg':
            self.pooling = torch.nn.AdaptiveAvgPool1d(1)
        elif pooling_type == 'max':
            self.pooling = torch.nn.AdaptiveMaxPool1d(1)
        else:
            raise ValueError('Pooling type chosen is not valid. Must be either `avg` or `max`')

        self.decoder_layers = torch.nn.Sequential(torch.nn.Linear(self._feat_in, self._num_classes, bias=True))
        self.apply(lambda x: init_weights(x, mode=init_mode))

    @typecheck()
    def forward(self, encoder_output):
        batch, in_channels, timesteps = encoder_output.size()

        encoder_output = self.pooling(encoder_output).view(batch, in_channels)  # [B, C]
        logits = self.decoder_layers(encoder_output)  # [B, num_classes]

        if self._return_logits:
            return logits

        return torch.nn.functional.softmax(logits, dim=-1)

    @property
    def num_classes(self):
        return self._num_classes


class ECAPA_Encoder(NeuralModule, Exportable):
    """
    Modified ECAPA Encoder layer without Res2Net module for faster training and inference which achieves
    better numbers on speaker diarization tasks

    input:
        feat_in: input feature shape (mel spec feature shape)
        filters: list of filter shapes for SE_TDNN modules 
        kernel_sizes: list of kernel shapes for SE_TDNN modules
        dilations: list of dilations for group conv se layer
        scale: scale value to group wider conv channels (deafult:8)
    
    output:
        outputs : encoded output 
        output_length: masked output lengths
    """

    @property
    def input_types(self):
        """Returns definitions of module input ports.
        """
        return OrderedDict(
            {
                "audio_signal": NeuralType(('B', 'D', 'T'), SpectrogramType()),
                "length": NeuralType(tuple('B'), LengthsType()),
            }
        )

    @property
    def output_types(self):
        """Returns definitions of module output ports.
        """
        return OrderedDict(
            {
                "outputs": NeuralType(('B', 'D', 'T'), AcousticEncodedRepresentation()),
                "encoded_lengths": NeuralType(tuple('B'), LengthsType()),
            }
        )

    def __init__(self, feat_in, filters, kernel_sizes, dilations, scale=8, init_mode='xavier_uniform'):
        super(ECAPA_Encoder, self).__init__()
        self.layers = nn.ModuleList()
        self.layers.append(TDNN_Module(feat_in, filters[0], kernel_size=kernel_sizes[0], dilation=dilations[0]))

        for i in range(len(filters) - 2):
            self.layers.append(
                SE_TDNN_Module(
                    filters[i],
                    filters[i + 1],
                    group_scale=scale,
                    se_channels=128,
                    kernel_size=kernel_sizes[i + 1],
                    dilation=dilations[i + 1],
                )
            )
        self.feature_agg = TDNN_Module(filters[-1], filters[-1], kernel_sizes[-1], dilations[-1])
        self.apply(lambda x: init_weights(x, mode=init_mode))

    def forward(self, audio_signal, length=None):
        x = audio_signal
        outputs = []

        for layer in self.layers:
            x = layer(x, length=length)
            outputs.append(x)

        x = torch.cat(outputs[1:], dim=1)
        x = self.feature_agg(x)
        return x, length


class SpeakerDecoder(NeuralModule, Exportable):
    """
    Speaker Decoder creates the final neural layers that maps from the outputs
    of Jasper Encoder to the embedding layer followed by speaker based softmax loss.
    Args:
        feat_in (int): Number of channels being input to this module
        num_classes (int): Number of unique speakers in dataset
        emb_sizes (list) : shapes of intermediate embedding layers (we consider speaker embbeddings from 1st of this layers)
                Defaults to [1024,1024]
        pool_mode (str) : Pooling stratergy type. options are 'xvector','tap', 'ecapa'
                Defaults to 'xvector'
        init_mode (str): Describes how neural network parameters are
            initialized. Options are ['xavier_uniform', 'xavier_normal',
            'kaiming_uniform','kaiming_normal'].
            Defaults to "xavier_uniform".
    """

    def input_example(self):
        """
        Generates input examples for tracing etc.
        Returns:
            A tuple of input examples.
        """
        input_example = torch.randn(16, self.input_feat_in, 256).to(next(self.parameters()).device)
        return tuple([input_example])

    @property
    def input_types(self):
        return OrderedDict(
            {
                "encoder_output": NeuralType(('B', 'D', 'T'), AcousticEncodedRepresentation()),
                "length": NeuralType(('B',), LengthsType()),
            }
        )

    @property
    def output_types(self):
        return OrderedDict(
            {
                "logits": NeuralType(('B', 'D'), LogitsType()),
                "embs": NeuralType(('B', 'D'), AcousticEncodedRepresentation()),
            }
        )

    def __init__(
        self,
        feat_in,
        num_classes,
        emb_sizes=[256],
        pool_mode='xvector',
        angular=False,
        attention_channels=128,
        init_mode="xavier_uniform",
    ):
        super(SpeakerDecoder, self).__init__()
        self.angular = angular
        self.emb_id = 2
        bias = False if self.angular else True
        emb_sizes = [emb_sizes] if type(emb_sizes) is int else emb_sizes

        self._num_classes = num_classes
        self.pool_mode = pool_mode.lower()
        if self.pool_mode == 'xvector' or self.pool_mode == 'tap':
            self._pooling = StatsPoolLayer(feat_in=feat_in, pool_mode=self.pool_mode)
            affine_type = 'linear'
        elif self.pool_mode == 'attention':
            self._pooling = AttentivePoolLayer(inp_filters=feat_in, attention_channels=attention_channels)
            affine_type = 'conv'

        shapes = [self._pooling.feat_in]
        for size in emb_sizes:
            shapes.append(int(size))

        emb_layers = []
        for shape_in, shape_out in zip(shapes[:-1], shapes[1:]):
            layer = self.affineLayer(shape_in, shape_out, learn_mean=False, affine_type=affine_type)
            emb_layers.append(layer)

        self.emb_layers = nn.ModuleList(emb_layers)

        self.final = nn.Linear(shapes[-1], self._num_classes, bias=bias)

        self.apply(lambda x: init_weights(x, mode=init_mode))

    def affineLayer(
        self, inp_shape, out_shape, learn_mean=True, affine_type='conv',
    ):
        if affine_type == 'conv':
            layer = nn.Sequential(
                nn.BatchNorm1d(inp_shape, affine=True, track_running_stats=True),
                nn.Conv1d(inp_shape, out_shape, kernel_size=1),
            )

        else:
            layer = nn.Sequential(
                nn.Linear(inp_shape, out_shape),
                nn.BatchNorm1d(out_shape, affine=learn_mean, track_running_stats=True),
                nn.ReLU(),
            )

        return layer

    @typecheck()
    def forward(self, encoder_output, length=None):
        pool = self._pooling(encoder_output, length)
        embs = []

        for layer in self.emb_layers:
            pool, emb = layer(pool), layer[: self.emb_id](pool)
            embs.append(emb)

        pool = pool.squeeze(-1)
        if self.angular:
            for W in self.final.parameters():
                W = F.normalize(W, p=2, dim=1)
            pool = F.normalize(pool, p=2, dim=1)

        out = self.final(pool)

        return out, embs[-1].squeeze(-1)


@dataclass
class JasperEncoderConfig:
    filters: int = MISSING
    repeat: int = MISSING
    kernel: List[int] = MISSING
    stride: List[int] = MISSING
    dilation: List[int] = MISSING
    dropout: float = MISSING
    residual: bool = MISSING

    # Optional arguments
    groups: int = 1
    separable: bool = False
    heads: int = -1
    residual_mode: str = "add"
    residual_dense: bool = False
    se: bool = False
    se_reduction_ratio: int = 8
    se_context_size: int = -1
    se_interpolation_mode: str = 'nearest'
    kernel_size_factor: float = 1.0
    stride_last: bool = False


@dataclass
class ConvASREncoderConfig:
    _target_: str = 'nemo.collections.asr.modules.ConvASREncoder'
    jasper: Optional[JasperEncoderConfig] = field(default_factory=list)
    activation: str = MISSING
    feat_in: int = MISSING
    normalization_mode: str = "batch"
    residual_mode: str = "add"
    norm_groups: int = -1
    conv_mask: bool = True
    frame_splicing: int = 1
    init_mode: Optional[str] = "xavier_uniform"


@dataclass
class ConvASRDecoderConfig:
    _target_: str = 'nemo.collections.asr.modules.ConvASRDecoder'
    feat_in: int = MISSING
    num_classes: int = MISSING
    init_mode: Optional[str] = "xavier_uniform"
    vocabulary: Optional[List[str]] = field(default_factory=list)


@dataclass
class ConvASRDecoderClassificationConfig:
    _target_: str = 'nemo.collections.asr.modules.ConvASRDecoderClassification'
    feat_in: int = MISSING
    num_classes: int = MISSING
    init_mode: Optional[str] = "xavier_uniform"
    return_logits: bool = True
    pooling_type: str = 'avg'

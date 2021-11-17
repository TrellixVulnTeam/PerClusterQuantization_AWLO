from operator import itemgetter

import torch.nn as nn
import torch
import torch.nn.functional as F

from ..quant_noise import _quant_noise
from ..quantization_utils import *
from .activation import *


class QuantizedConv2d(nn.Conv2d):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, activation=None,
                 dilation=1, groups=1, bias=False, is_first=False, multiplication=True, arg_dict=None):
        super(QuantizedConv2d, self).__init__(in_channels, out_channels, kernel_size, stride,
                                              padding, dilation, groups, bias)
        self.layer_type = 'QuantizedConv2d'
        self.base, bit, self.num_clusters, self.runtime_helper, self.default_batch = \
            itemgetter('quant_base', 'bit', 'cluster', 'runtime_helper', 'val_batch')(arg_dict)
        self.w_bit = nn.Parameter(torch.tensor(bit, dtype=torch.int8), requires_grad=False)
        self.a_bit = nn.Parameter(torch.tensor(bit, dtype=torch.int8), requires_grad=False)
        self.is_first = is_first
        self.is_bias = nn.Parameter(torch.tensor(False, dtype=torch.bool), requires_grad=False)
        self.quantized_bias = nn.Parameter(torch.zeros((self.num_clusters, out_channels), dtype=torch.int32), requires_grad=False)
        self.sum_a2 = nn.Parameter(torch.zeros((1, out_channels, 1, 1), dtype=torch.int32), requires_grad=False)
        self.sum_a1, self.total = None, None  # for faster inference

        self.out_channels = out_channels
        self.multiplication = multiplication

        t_init = list(range(self.num_clusters)) if self.num_clusters > 1 else 0
        self.s1 = nn.Parameter(torch.tensor(t_init, dtype=torch.float32), requires_grad=False)
        self.s2 = nn.Parameter(torch.tensor(0, dtype=torch.float32), requires_grad=False)
        self.s3 = nn.Parameter(torch.tensor(t_init, dtype=torch.float32), requires_grad=False)
        self.z1 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.z2 = nn.Parameter(torch.tensor(0, dtype=torch.int32), requires_grad=False)
        self.z3 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.M0 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.shift = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.is_shift_neg = nn.Parameter(torch.tensor(False, dtype=torch.bool), requires_grad=False)
        self.hardswish_6 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.hardswish_3 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.s_activation = nn.Parameter(torch.tensor(t_init, dtype=torch.float32), requires_grad=False)
        self.z_activation = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.activation = activation

    def forward(self, x):
        x, out = self._conv_impl(x)
        out = self._subsum(x, out)
        if self.multiplication:
            out = self._totalsum(out)
        return out

    def _conv_impl(self, x):
        padded = x

        # Pad if needed
        if self.padding[0] > 0:
            to_pad = (self.padding[0], self.padding[0], self.padding[1], self.padding[1])

            # If Non-DAQ,
            if self.num_clusters == 1:
                padded = F.pad(x, to_pad, mode='constant', value=self.z1.item())
            else:  # DAQ
                bc = self.runtime_helper.batch_cluster
                if self.is_first or self.w_bit == 8:
                    exists = torch.unique(bc)
                    padded = torch.zeros(
                        (x.shape[0], x.shape[1], x.shape[2] + self.padding[0] * 2, x.shape[3] + self.padding[1] * 2),
                        device='cuda')
                    for c in exists:
                        indices = (bc == c).nonzero(as_tuple=True)[0]
                        padded[indices] = F.pad(x[indices], to_pad, mode='constant', value=self.z1[c])
                else:
                    padded = F.pad(x, to_pad, mode='constant', value=0)

        out = F.conv2d(padded, self.weight, None, self.stride, (0, 0), self.dilation, self.groups)
        return padded.type(torch.cuda.IntTensor), out.type(torch.cuda.LongTensor)

    def _subsum(self, x, y):
        if self.num_clusters > 1:
            return self._pcq_subsum(x, y)
        else:
            return self._general_subsum(x, y)

    def _totalsum(self, x):
        if self.num_clusters > 1:
            out = self._pcq_totalsum(x)
        else:
            out = self._general_totalsum(x)
        return clamp_matrix(out, self.a_bit)

    def _pcq_subsum(self, x, sum_q1q2):
        batch_size = x.size(0)
        bc = self.runtime_helper.batch_cluster
        z1 = torch.index_select(self.z1, 0, bc)[:, None, None, None]

        input_batch, input_ch, input_col, input_row = x.shape[0], x.shape[1], x.shape[2], x.shape[3]
        filter_batch, filter_ch, filter_col, filter_row =\
            self.weight.shape[0], self.weight.shape[1], self.weight.shape[2], self.weight.shape[3]
        stride = self.stride[0]

        if self.is_bias:
            bias = torch.index_select(self.quantized_bias, 0, bc)
            sum_q1q2 = sum_q1q2.add(bias[:, :, None, None])

        output_col = sum_q1q2.shape[2]
        output_row = sum_q1q2.shape[3]
        if self.sum_a1 is None:
            self.sum_a1 = torch.zeros((input_batch, output_col, output_row), dtype=torch.int32, device='cuda')

        for o_col in range(output_col):
            for o_row in range(output_row):
                col_st, col_end = o_col * stride, o_col * stride + filter_col
                row_st, row_end = o_row * stride, o_row * stride + filter_row
                self.sum_a1[:batch_size, o_col, o_row] = torch.sum(x[:, :, col_st: col_end, row_st: row_end], (1, 2, 3))
        sum_a1 = self.sum_a1[:batch_size] * self.z2
        sum_a2 = self.sum_a2.mul(z1)

        nz1z2 = input_ch * filter_col * filter_row * z1 * self.z2
        subsum = sum_q1q2.add(nz1z2)
        subsum = torch.sub(subsum, sum_a1[:, None, :, :])
        subsum = torch.sub(subsum, sum_a2)
        return subsum

    def _pcq_totalsum(self, subsum):
        batch_size = subsum.size(0)
        bc = self.runtime_helper.batch_cluster
        z3 = torch.index_select(self.z3, 0, bc)[:, None, None, None]
        M0 = torch.index_select(self.M0, 0, bc)[:, None, None, None]
        shift = torch.index_select(self.shift, 0, bc)[:, None, None, None]

        if not self.is_shift_neg:
            multiplied = multiply_M(subsum, M0)
            total = shifting4d(multiplied, shift,
                               self.runtime_helper.mask_4d[:batch_size],
                               self.runtime_helper.zero_4d[:batch_size],
                               self.runtime_helper.one_4d[:batch_size])
            total = total.add(z3)
        else:
            if self.total is None:
                self.total = torch.zeros(subsum.shape, dtype=torch.int64, device='cuda')
            neg = (shift < 0).nonzero(as_tuple=True)[0]
            pos = (shift >= 0).nonzero(as_tuple=True)[0]
            n_pos = len(pos)
            if len(neg) > 0:
                multiplied = multiply_M((subsum[neg] << - shift[neg]), M0[neg])
                self.total[neg] = shifting_without_cast(multiplied, 0)
            if n_pos > 0:
                multiplied = multiply_M(subsum[pos], M0[pos])
                self.total[pos] = shifting4d_without_cast(multiplied, shift[pos],
                                                          self.runtime_helper.mask_4d[:n_pos],
                                                          self.runtime_helper.zero_4d[:n_pos],
                                                          self.runtime_helper.one_4d[:n_pos])
            total = self.total[:batch_size].add(z3)
        return total

    def _general_subsum(self, x, sum_q1q2):
        input_batch, input_ch, input_col, input_row = x.shape[0], x.shape[1], x.shape[2], x.shape[3]
        filter_batch, filter_ch, filter_col, filter_row = self.weight.shape[0], self.weight.shape[1], self.weight.shape[2], self.weight.shape[3]
        stride = self.stride[0]

        if self.is_bias:
            sum_q1q2 = sum_q1q2.add(self.quantized_bias[0][None, :, None, None])
        output_col = sum_q1q2.shape[2]
        output_row = sum_q1q2.shape[3]

        if self.sum_a1 is None:
            self.sum_a1 = torch.zeros((input_batch, output_col, output_row), dtype=torch.int32, device='cuda')
        for o_col in range(output_col):
            for o_row in range(output_row):
                col_st, col_end = o_col * stride, o_col * stride + filter_col
                row_st, row_end = o_row * stride, o_row * stride + filter_row
                self.sum_a1[:x.size(0), o_col, o_row] = torch.sum(x[:, :, col_st: col_end, row_st: row_end], (1, 2, 3))
        sum_a1 = self.sum_a1[:x.size(0)].mul(self.z2)

        sum_a2 = self.sum_a2.mul(self.z1)
        nz1z2 = input_ch * filter_col * filter_row * self.z1 * self.z2
        subsum = sum_q1q2.add(nz1z2)
        subsum = torch.sub(subsum, sum_a1[:, None, :, :])
        subsum = torch.sub(subsum, sum_a2)
        return subsum

    def _general_totalsum(self, subsum):
        if self.shift < 0:
            multiplied = multiply_M((subsum << - self.shift.item()), self.M0)
            total = shifting_without_cast(multiplied, 0)
        else:
            multiplied = multiply_M(subsum, self.M0)
            total = shifting_without_cast(multiplied, self.shift.item())
        return total.add(self.z3)


class PCQConv2d(nn.Module):
    """
        Fused Layer to calculate Quantization Parameters(S & Z) with multiple clusters
    """
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, groups=1, dilation=1,
                 bias=False, activation=None, w_bit=None, a_bit=None, arg_dict=None):
        super(PCQConv2d, self).__init__()
        self.layer_type = 'PCQConv2d'
        self.out_channels = out_channels
        self.groups = groups

        self.smooth, self.num_clusters, self.runtime_helper, self.use_ste, self.quant_noise, self.qn_prob, self.qn_each_channel\
            = itemgetter('smooth', 'cluster', 'runtime_helper', 'ste', 'quant_noise', 'qn_prob', 'qn_each_channel')(arg_dict)

        w_bit = w_bit if w_bit is not None else arg_dict['bit']
        a_bit = a_bit if a_bit is not None else arg_dict['bit']
        self.w_bit = torch.nn.Parameter(torch.tensor(w_bit, dtype=torch.int8), requires_grad=False)
        self.a_bit = torch.nn.Parameter(torch.tensor(a_bit, dtype=torch.int8), requires_grad=False)

        self.act_range = nn.Parameter(torch.zeros((self.num_clusters, 2)), requires_grad=False)
        self.apply_ema = nn.Parameter(torch.zeros(self.num_clusters, dtype=torch.bool), requires_grad=False)

        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, padding=padding,
                              groups=groups,  bias=bias, dilation=dilation)

        self._activation = activation(inplace=True) if activation else None
        self.out_channels = out_channels
        self.in_channels = in_channels

    def forward(self, x, external_range=None):
        if not self.training:
            return self._forward_impl(x)

        out = self._pcq(x)
        if external_range is None:
            self._update_activation_ranges(out)
        if self.runtime_helper.apply_fake_quantization:
            out = self._fake_quantize_activation(out, external_range)
        return out

    def _forward_impl(self, x):
        x = self.conv(x)
        if self._activation:
            x = self._activation(x)
        return x

    def _pcq(self, x):
        s, z = calc_qparams(self.conv.weight.detach().min(), self.conv.weight.detach().max(), self.w_bit)
        if not self.quant_noise:
            w = fake_quantize(self.conv.weight, s, z, self.w_bit, use_ste=self.use_ste)
        else:
            w = apply_qn(self.conv.weight, scale=s, zero_point=z, w_bit=self.w_bit, qn_prob=self.qn_prob,
                         kernel_size=self.conv.kernel_size, each_channel=self.qn_each_channel,
                         in_feature=self.in_channels, out_feature=self.out_channels)
        out = F.conv2d(x, w, self.conv.bias, self.conv.stride, self.conv.padding, self.conv.dilation, self.conv.groups)
        if self._activation:
            out = self._activation(out)
        return out

    @torch.no_grad()
    def _update_activation_ranges(self, x):
        cluster = self.runtime_helper.batch_cluster
        data = x.view(x.size(0), -1)
        _max = data.max(dim=1).values.mean()
        if self._activation:
            if self.apply_ema[cluster]:
                self.act_range[cluster][1] = self.act_range[cluster][1] * self.smooth + _max * (1 - self.smooth)
            else:
                self.act_range[cluster][1] = _max
                self.apply_ema[cluster] = True
        else:
            _min = data.min(dim=1).values.mean()
            if self.apply_ema[cluster]:
                self.act_range[cluster][0] = self.act_range[cluster][0] * self.smooth + _min * (1 - self.smooth)
                self.act_range[cluster][1] = self.act_range[cluster][1] * self.smooth + _max * (1 - self.smooth)
            else:
                self.act_range[cluster][0], self.act_range[cluster][1] = _min, _max
                self.apply_ema[cluster] = True

    def _fake_quantize_activation(self, x, external_range=None):
        cluster = self.runtime_helper.batch_cluster
        if external_range is not None:
            s, z = calc_qparams(external_range[cluster][0], external_range[cluster][1], self.a_bit)
        else:
            s, z = calc_qparams(self.act_range[cluster][0], self.act_range[cluster][1], self.a_bit)
        return fake_quantize(x, s, z, self.a_bit, use_ste=self.use_ste)

    @torch.no_grad()
    def set_qparams(self, s1, z1, s_external=None, z_external=None):
        self.s1, self.z1 = s1, z1
        self.s2, self.z2 = calc_qparams(self.conv.weight.min(), self.conv.weight.max(), self.w_bit)
        if s_external is not None:
            self.s3, self.z3 = s_external, z_external
        else:
            self.s3, self.z3 = calc_qparams_per_cluster(self.act_range, self.a_bit)

        self.M0 = torch.zeros(self.num_clusters, dtype=torch.int32)
        self.shift = torch.zeros(self.num_clusters, dtype=torch.int32)
        for c in range(self.num_clusters):
            self.M0[c], self.shift[c] = quantize_M(self.s1[c] * self.s2 / self.s3[c])
        return self.s3, self.z3


class FusedConv2d(nn.Module):
    """
        Fused Layer to calculate Quantization Parameters (S & Z)
    """
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, dilation=1, groups=1, bias=False,
                 norm_layer=None, activation=None, w_bit=None, a_bit=None, arg_dict=None):
        super(FusedConv2d, self).__init__()
        self.layer_type = 'FusedConv2d'
        self.groups = groups

        self.arg_dict = arg_dict
        self.smooth, self.fold_convbn, self.use_ste, self.runtime_helper, self.quant_noise, self.qn_prob, self.qn_each_channel\
            = itemgetter('smooth', 'fold_convbn', 'ste', 'runtime_helper', 'quant_noise', 'qn_prob', 'qn_each_channel')(arg_dict)
        
        w_bit = w_bit if w_bit is not None else arg_dict['bit']
        a_bit = a_bit if a_bit is not None else arg_dict['bit']
        self.w_bit = torch.nn.Parameter(torch.tensor(w_bit, dtype=torch.int8), requires_grad=False)
        self.a_bit = torch.nn.Parameter(torch.tensor(a_bit, dtype=torch.int8), requires_grad=False)

        self.act_range = nn.Parameter(torch.zeros(2), requires_grad=False)
        self.apply_ema = nn.Parameter(torch.zeros(1, dtype=torch.bool), requires_grad=False)

        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, stride=stride, padding=padding,
                              groups=self.groups, bias=bias, dilation=dilation)
        self._norm_layer = norm_layer(out_channels) if norm_layer else None
        self._activation = activation(inplace=False) if activation else None
        self.out_channels = out_channels
        self.in_channels = in_channels

    def forward(self, x, external_range=None):
        if not self.training:
            x = self.conv(x)
            if self._norm_layer:
                x = self._norm_layer(x)
            if self._activation:
                x = self._activation(x)
            return x

        if self.fold_convbn:
            return self._norm_folded(x, external_range)
        else:
            return self._general(x, external_range)

    def _general(self, x, external_range=None):
        s, z = calc_qparams(self.conv.weight.detach().min(), self.conv.weight.detach().max(), self.w_bit)
        if not self.quant_noise:
            w = fake_quantize(self.conv.weight, s, z, self.w_bit, self.use_ste)
        else:
            w = apply_qn(self.conv.weight, s, z, self.w_bit, qn_prob=self.qn_prob,
                         kernel_size=self.conv.kernel_size, each_channel=self.qn_each_channel,
                         in_feature=self.in_channels, out_feature=self.out_channels)

        out = F.conv2d(x, w, self.conv.bias, self.conv.stride, self.conv.padding, self.conv.dilation, self.conv.groups)
        if self._activation:
            out = self._activation(out)

        if external_range is not None:
            if self.runtime_helper.apply_fake_quantization:
                s, z = calc_qparams(external_range[0], external_range[1], self.a_bit)
                out = fake_quantize(out, s, z, self.a_bit, self.use_ste)
        else:
            if self.apply_ema:
                self.act_range[0], self.act_range[1] = ema(out, self.act_range, self.smooth)
                if self.runtime_helper.apply_fake_quantization:
                    s, z = calc_qparams(self.act_range[0], self.act_range[1], self.a_bit)
                    out = fake_quantize(out, s, z, self.a_bit, self.use_ste)
            else:
                self.act_range[0], self.act_range[1] = get_range(out)
                self.apply_ema.data = torch.tensor(True, dtype=torch.bool)
        return out

    def _norm_folded(self, x, external_range=None):
        general_out = self.conv(x)
        general_out = self._norm_layer(general_out)
        if self._activation:
            general_out = self._activation(general_out)

        with torch.no_grad():
            alpha, beta, mean, var, eps = self._norm_layer.weight, self._norm_layer.bias, self._norm_layer.running_mean, \
                                          self._norm_layer.running_var, self._norm_layer.eps
            n_channel = self.conv.weight.shape[0]

            folded_weight = self.conv.weight.clone().detach()
            folded_bias = beta.clone().detach()
            for c in range(n_channel):
                folded_weight.data[c] = folded_weight.data[c].mul(alpha[c]).div(torch.sqrt(var[c].add(eps)))
                folded_bias.data[c] = folded_bias.data[c].sub(alpha[c].mul(mean[c]).div(torch.sqrt(var[c])))

            s, z = calc_qparams(torch.min(folded_weight), torch.max(folded_weight), self.w_bit)
            fq_folded_weight = fake_quantize(folded_weight, s, z, self.w_bit, use_ste=False)

            folded_out = F.conv2d(x, fq_folded_weight, folded_bias, self.conv.stride, self.conv.padding,
                                  self.conv.dilation, self.conv.groups)
            if self._activation:
                folded_out = self._activation(folded_out)

            if external_range is None:
                if self.apply_ema:
                    self.act_range[0], self.act_range[1] = ema(folded_out, self.act_range, self.smooth)
                    if self.runtime_helper.apply_fake_quantization:
                        s, z = calc_qparams(self.act_range[0], self.act_range[1], self.a_bit)
                        folded_out = fake_quantize(folded_out, s, z, self.a_bit, use_ste=False)
                else:
                    self.act_range[0] = torch.min(folded_out).item()
                    self.act_range[1] = torch.max(folded_out).item()
                    self.apply_ema.data = torch.tensor(True, dtype=torch.bool)
            else:
                if self.runtime_helper.apply_fake_quantization:
                    s, z = calc_qparams(external_range[0], external_range[1], self.a_bit)
                    folded_out = fake_quantize(folded_out, s, z, self.a_bit, use_ste=False)
        return STE.apply(general_out, folded_out)

    def fold_conv_and_bn(self):
        # In case of validation, fuse pretrained Conv&BatchNorm params
        assert self.training == False, 'Do not fuse layers while training.'
        alpha, beta, mean, var, eps = self._norm_layer.weight, self._norm_layer.bias, self._norm_layer.running_mean,\
                                      self._norm_layer.running_var, self._norm_layer.eps
        n_channel = self.conv.weight.shape[0]
        self.conv.bias = nn.Parameter(beta)
        for c in range(n_channel):
            self.conv.weight.data[c] = self.conv.weight.data[c].mul(alpha[c]).div(torch.sqrt(var[c].add(eps)))
            self.conv.bias.data[c] = self.conv.bias.data[c].sub(alpha[c].mul(mean[c]).div(torch.sqrt(var[c])))
        self._norm_layer = nn.Identity()

    def set_qparams(self, s1, z1, s_external=None, z_external=None):
        self.s1, self.z1 = s1, z1
        self.s2, self.z2 = calc_qparams(self.conv.weight.min(), self.conv.weight.max(), self.w_bit)

        if s_external is not None:
            self.s3, self.z3 = s_external, z_external
        else:
            self.s3, self.z3 = calc_qparams(self.act_range[0], self.act_range[1], self.a_bit)

        self.M0, self.shift = quantize_M(self.s1 * self.s2 / self.s3)
        return self.s3, self.z3


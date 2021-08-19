from operator import itemgetter

import torch.nn as nn
import torch

from ..quantization_utils import *


class QuantizedBn2d(nn.Module):
    def __init__(self, num_features, arg_dict=None):
        super(QuantizedBn2d, self).__init__()
        self.layer_type = 'QuantizedBn2d'
        self.bit, self.num_clusters, self.runtime_helper = itemgetter('bit', 'cluster', 'runtime_helper')(arg_dict)
        self.q_max = 2 ** self.bit - 1
        self.num_features = num_features

        t_init = list(range(self.num_clusters)) if self.num_clusters > 1 else 0
        self.s1 = nn.Parameter(torch.tensor(t_init, dtype=torch.float32), requires_grad=False)
        self.s2 = nn.Parameter(torch.tensor(t_init, dtype=torch.float32), requires_grad=False)
        self.s3 = nn.Parameter(torch.tensor(t_init, dtype=torch.float32), requires_grad=False)
        self.z1 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.z2 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.z3 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.M0 = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)
        self.shift = nn.Parameter(torch.tensor(t_init, dtype=torch.int32), requires_grad=False)

        self.weight = nn.Parameter(torch.zeros((self.num_clusters, num_features), dtype=torch.int32), requires_grad=False)
        self.bias = nn.Parameter(torch.zeros((self.num_clusters, num_features), dtype=torch.int32), requires_grad=False)

    def forward(self, x):
        if self.runtime_helper.batch_cluster is not None:
            return self._pcq(x.type(torch.cuda.LongTensor))
        else:
            return self._general(x.type(torch.cuda.LongTensor))

    def _pcq(self, x):
        bc = self.runtime_helper.batch_cluster
        _size = x.shape[-1]
        done = 0
        total = torch.zeros(x.shape, dtype=torch.int32).cuda()
        for i in range(bc.shape[0]):
            c = bc[i][0].item()
            n = bc[i][1].item()
            weight = self.weight[c].repeat_interleave(_size * _size)\
                                   .reshape(self.num_features, _size, _size)\
                                   .repeat(n, 1, 1, 1)
            bias = self.bias[c].repeat_interleave(_size * _size)\
                               .reshape(self.num_features, _size, _size)\
                               .repeat(n, 1, 1, 1)
            q1q2 = x[done:done + n].mul(weight)
            q1z2 = x[done:done + n].mul(self.z2[c])
            q2z1 = weight.mul(self.z1[c])
            subsum = q1q2 - q1z2 - q2z1 + self.z1[c] * self.z2[c] + bias

            if self.shift[c] < 0:
               subsum = multiply_M((subsum << - self.shift[c].item()), self.M0[c])
               subsum = shifting(subsum, 0)
            else:
               subsum = multiply_M(subsum, self.M0[c])
               subsum = shifting(subsum, self.shift[c].item())
            total[done:done + n] = subsum.add(self.z3[c])
            done += n

        if self.bit == 4:
            total = torch.clamp(total, 0, 15)
        else:
            total = torch.clamp(total, -128, 127)
        return total.type(torch.cuda.FloatTensor)

    def general(self, x):
        _size = x.shape[-1]
        weight = self.weight[0].repeat_interleave(_size * _size)\
                               .reshape(self.num_features, _size, _size)\
                               .repeat(x.shape[0], 1, 1, 1)
        bias = self.bias[0].repeat_interleave(_size * _size)\
                           .reshape(self.num_features, _size, _size)\
                           .repeat(x.shape[0], 1, 1, 1)
        q1q2 = x.mul(weight)
        q1z2 = x.mul(self.z2)
        q2z1 = weight.mul(self.z1)
        subsum = q1q2 - q1z2 - q2z1 + self.z1 * self.z2 + bias

        if self.shift.item() < 0:
           subsum = multiply_M((subsum << - self.shift.item()), self.M0)
           subsum = shifting(subsum, 0)
        else:
           subsum = multiply_M(subsum, self.M0)
           subsum = shifting(subsum, self.shift.item())
        total = subsum.add(self.z3)

        if self.bit == 4:
            total = torch.clamp(total, 0, 15)
        else:
            total = torch.clamp(total, -128, 127)
        return total.type(torch.cuda.FloatTensor)


class PCQBnReLU(nn.Module):
    def __init__(self, num_features, activation=None, arg_dict=None):
        super(PCQBnReLU, self).__init__()
        self.layer_type = 'PCQBnReLU'
        self.runtime_helper, self.num_clusters = itemgetter('runtime_helper', 'cluster')(arg_dict)

        self.norms = nn.ModuleList([FusedBnReLU(num_features, activation=activation, arg_dict=arg_dict)\
                                    for _ in range(self.num_clusters)])

    def forward(self, x):
        done = 0
        out = []
        for i in range(self.runtime_helper.batch_cluster.shape[0]):
            c = self.runtime_helper.batch_cluster[i][0].item()
            n = self.runtime_helper.batch_cluster[i][1].item()
            out.append(self.norms[c](x[done:done + n]))
            done += n
        return torch.cat(out)

    def set_qparams(self, s1, z1):
        self.s1, self.z1 = nn.Parameter(s1, requires_grad=False), nn.Parameter(z1, requires_grad=False)

        self.s2 = nn.Parameter(torch.zeros(self.num_clusters, dtype=torch.float32), requires_grad=False)
        self.s3 = nn.Parameter(torch.zeros(self.num_clusters, dtype=torch.float32), requires_grad=False)
        self.z2 = nn.Parameter(torch.zeros(self.num_clusters, dtype=torch.int32), requires_grad=False)
        self.z3 = nn.Parameter(torch.zeros(self.num_clusters, dtype=torch.int32), requires_grad=False)
        self.M0 = nn.Parameter(torch.zeros(self.num_clusters, dtype=torch.int32), requires_grad=False)
        self.shift = nn.Parameter(torch.zeros(self.num_clusters, dtype=torch.int32), requires_grad=False)
        for c in range(self.num_clusters):
            self.s3[c], self.z3[c] = self.norms[c].set_qparams(self.s1[c], self.z1[c])
            self.s2[c], self.z2[c] = self.norms[c].get_weight_qparams()
            self.M0[c], self.shift[c] = self.norms[c].get_multiplier_qparams()
        return self.s3, self.z3

    def fold_norms(self):
        for c in range(self.num_clusters):
            self.norms[c].fold_bn()


class FusedBnReLU(nn.Module):
    def __init__(self, num_features, activation=None, arg_dict=None):
        super(FusedBnReLU, self).__init__()
        self.layer_type = 'FusedBnReLU'
        self.bit, self.smooth, self.use_ste, self.runtime_helper = \
            itemgetter('bit', 'smooth', 'ste', 'runtime_helper')(arg_dict)
        self.q_max = 2 ** self.bit - 1
        # self.w_qmax = 2 ** 32 - 1
        self.w_qmax = 2 ** 4 - 1

        self.act_range = nn.Parameter(torch.zeros(2), requires_grad=False)
        self.apply_ema = False

        self.bn = nn.BatchNorm2d(num_features)
        self._activation = activation(inplace=True) if activation else None

    def forward(self, x):
        x = self.bn(x)
        if self._activation is not None:
            x = self._activation(x)
        if not self.training:
            return x

        out = x
        if self.apply_ema:
            self.act_range[0], self.act_range[1] = ema(x, self.act_range, self.smooth)
            if self.runtime_helper.apply_fake_quantization:
                s, z = calc_qparams(self.act_range[0], self.act_range[1], self.q_max)
                out = fake_quantize(x, s, z, self.q_max, self.use_ste)
        else:
            self.act_range[0] = torch.min(x).item()
            self.act_range[1] = torch.max(x).item()
            self.apply_ema = True
        return out

    def fold_bn(self):
        # In case of validation, fuse pretrained Conv&BatchNorm params
        assert self.training == False, 'Do not fuse layers while training.'
        alpha, beta, mean, var, eps = self.bn.weight, self.bn.bias, self.bn.running_mean,\
                                      self.bn.running_var, self.bn.eps
        self.weight = nn.Parameter(alpha / torch.sqrt(var + eps), requires_grad=False)
        self.bias = nn.Parameter(beta - alpha * mean / torch.sqrt(var + eps), requires_grad=False)

    def set_qparams(self, s1, z1):
        self.s1, self.z1 = nn.Parameter(s1, requires_grad=False), nn.Parameter(z1, requires_grad=False)
        if self.weight.min() > 0:
            self.s2, self.z2 = calc_qparams(torch.tensor(0), self.weight.max(), self.w_qmax)
        elif self.weight.max() < 0:
            self.s2, self.z2 = calc_qparams(self.weight.min(), torch.tensor(0), self.w_qmax)
        else:
            self.s2, self.z2 = calc_qparams(self.weight.min(), self.weight.max(), self.w_qmax)
        self.s3, self.z3 = calc_qparams(self.act_range[0], self.act_range[1], self.q_max)
        self.M0, self.shift = quantize_M(self.s1 * self.s2 / self.s3)
        return self.s3, self.z3

    def get_weight_qparams(self):
        return self.s2, self.z2

    def get_multiplier_qparams(self):
        return self.M0, self.shift
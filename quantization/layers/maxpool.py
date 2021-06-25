import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
import sys
import psutil

from quantization.quantization_utils import *

class QuantizedMaxPool2d(nn.MaxPool2d):
    def __init__(self, kernel_size, stride=1, padding=0, quant_only=False):
        super(QuantizedMaxPool2d, self).__init__(kernel_size, stride, padding, quant_only)   

        self.layer_type = 'QuantizedMaxPool2d'     
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding
        self.maxpool = False
        self.zero_point = nn.Parameter(torch.tensor(0, dtype=torch.int32), requires_grad=False)

    def forward(self, x):
        if self.padding[0] > 0 or self.padding[1] > 0:
             x = F.pad(x, (self.padding[0], self.padding[0], self.padding[1], self.padding[1]), mode='constant', value=self.zero_point)

        self.maxpool = nn.MaxPool2d(self.kernel_size, self.stride, self.padding)
        out = self.maxpool(x)

        return out
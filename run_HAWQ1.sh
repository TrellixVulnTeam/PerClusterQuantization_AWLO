#! /bin/bash

PRETRAINED_MODEL_PATH="/workspace/pretrained_models"

MODEL="alexnet"
DATASET="cifar10"
PRETRAINED_MODEL="alexnet"

BATCH=32

CUDA_VISIBLE_DEVICES=1 python main.py \
    --mode fine \
    --epochs 100 \
    --batch $BATCH \
    --quant_base hawq \
    --arch $MODEL \
    --dataset $DATASET \
    --lr 0.001 \
    --act-range-momentum 0.99 \
    --wd 1e-4 \
    --fix-BN \
    --pretrained \
    --channel-wise true \
    --quant-scheme uniform4 \
    --gpu 0 \
    --data $DATASET \
    --transfer_param \
    --batch-size $BATCH \
    --dnn_path $PRETRAINED_MODEL_PATH/$DATASET/$PRETRAINED_MODEL/checkpoint.pth \
#    --cluster 4 \
#    --imagenet /workspace/dataset/
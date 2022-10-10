#! /bin/bash

SCRIPT_PATH="/workspace/PerClusterQuantization/run_HAWQ.sh"

GPU=${1}

MERGE_TYPES=(jaccard)
MODELS=(alexnet resnet20)
DATAS=(cifar10 cifar100 svhn)
SUB_CLUSTERS=(64 128 256 512)
FIN_CLUSTERS=(7 6 5 4 3 2)
LEARNING_RATES=(0.0001)
BATCH_SIZES=(128)

for MERGE_TYPE in ${MERGE_TYPES[@]}; do
    for MODEL in ${MODELS[@]}; do
        for DATA in ${DATAS[@]}; do
            for BATCH_SIZE in ${BATCH_SIZES[@]}; do
                for LEARNING_RATE in ${LEARNING_RATES[@]}; do
                    for SUB_CLUSTER in ${SUB_CLUSTERS[@]}; do
                        for FIN_CLUSTER in ${FIN_CLUSTERS[@]}; do
                            /bin/bash ${SCRIPT_PATH} ${GPU} ${MODEL} ${DATA} ${BATCH_SIZE} ${LEARNING_RATE} false $((SUB_CLUSTER/8*FIN_CLUSTER)) ${SUB_CLUSTER} ${MERGE_TYPE} false & wait
                        done
                    done
                done
            done
        done
    done
done
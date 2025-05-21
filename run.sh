#!/bin/bash

environments=("halfcheetah" "walker" "hopper" "ant")

for env in "${environments[@]}"
do
    command="nohup python run.py --config-path config_${env}_robust_q_ppo_sgld.json --exp-id A3B-stable-val-3 --acoe--norm-mode A3B --q-weight 0.2 --adam-eps 5e-4 --ppo-lr-adam 5e-4 --val-lr 5e-4 > a3b_stable-val-3_${env}.log 2>&1 &"
    echo $command >> experiment_history.log
    eval $command
done
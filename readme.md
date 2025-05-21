# ACOE: Adversarial Counterfactual Error

This repository contains code for training and evaluating robust reinforcement learning agents using Adversarial Counterfactual Error (ACoE) from our paper: On Minimizing Adversarial Counterfactual Error in Adversarial Reinforcement Learning. 

https://arxiv.org/abs/2406.04724 (Presented at ICLR 2025)

Contact: rbelaire dot 2021 at phdcs dot smu dot edu dot sg

## Setup and Installation

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Install auto_LiRPA:
   ```bash
   git clone https://github.com/KaidiXu/auto_LiRPA
   cd auto_LiRPA
   python setup.py install
   ```

## Key Operations

### 1. Training with ACOE

To train a robust model using ACOE:

```bash
python run.py --config-path configs/config_ant_pa_atla_ppo.json --acoe --norm-mode A3B
```

### 2. Evaluating ACOE Models

To evaluate an ACOE model under adversarial attack:

```bash
python test.py --config-path configs/config_ant_pa_atla_ppo.json --load-model models/acoe/acoe_ant.model --deterministic --attack-method action 
```

### 3. Extracting Trained Models

After training, extract the best model from the experiment directory:

```bash
python get_best_pickle.py path/to/experiment/directory --output acoe_ant.model
```

### Citation

If you find this code useful, please cite our paper:

```bibtex
@inproceedings{DBLP:conf/iclr/BelaireSV25,
  author       = {Roman Belaire and
                  Arunesh Sinha and
                  Pradeep Varakantham},
  title        = {On Minimizing Adversarial Counterfactual Error in Adversarial Reinforcement
                  Learning},
  booktitle    = {The Thirteenth International Conference on Learning Representations,
                  {ICLR} 2025, Singapore, April 24-28, 2025},
  publisher    = {OpenReview.net},
  year         = {2025},
  url          = {https://openreview.net/forum?id=eUEMjwh5wK},
  timestamp    = {Thu, 15 May 2025 17:19:05 +0200},
  biburl       = {https://dblp.org/rec/conf/iclr/BelaireSV25.bib},
  bibsource    = {dblp computer science bibliography, https://dblp.org}
}

```


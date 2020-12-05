import re
import math
import copy
import time
import imageio
import numpy as np
import matplotlib.pyplot as plt

from sklearn.preprocessing import MinMaxScaler, StandardScaler

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable


######## MODEL HELPERS ##########

def clones(module, N):
    "Produce N identical layers"
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])

def subsequent_mask(size):
    "Mask out subsequent positions"
    attn_shape = (1, size, size)
    subsequent_mask = np.triu(np.ones(attn_shape), k=1).astype('uint8')
    return torch.from_numpy(subsequent_mask) == 0

def attention(query, key, value, mask=None, dropout=None):
    "Compute 'Scaled Dot Product Attention'"
    d_k = query.size(-1)
    scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(d_k)
    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)
    p_attn = F.softmax(scores, dim=-1)
    if dropout is not None:
        p_attn = dropout(p_attn)
    return torch.matmul(p_attn, value), p_attn

class ListModule(nn.Module):
    def __init__(self, *args):
        super(ListModule, self).__init__()
        idx = 0
        for module in args:
            self.add_module(str(idx), module)
            idx += 1

    def __getitem__(self, idx):
        if idx < 0 or idx >= len(self._modules):
            raise IndexError('index {} is out of range'.format(idx))
        it = iter(self._modules.values())
        for i in range(idx):
            next(it)
        return next(it)

    def __iter__(self):
        return iter(self._modules.values())

    def __len__(self):
        return len(self._modules)

class KLAnnealer:
    def __init__(self, kl_low, kl_high, n_epochs, start_epoch):
        self.kl_low = kl_low
        self.kl_high = kl_high
        self.n_epochs = n_epochs
        self.start_epoch = start_epoch

        self.kl = (self.kl_high - self.kl_low) / (self.n_epochs - self.start_epoch)

    def __call__(self, epoch):
        k = (epoch - self.start_epoch) if epoch >= self.start_epoch else 0
        return self.kl_low + k * self.kl


####### PREPROCESSING HELPERS ##########

def smi_tokenizer(smile):
    pattern =  "(\[[^\]]+]|Br?|Cl?|N|O|S|P|F|I|b|c|n|o|s|p|\(|\)|\.|=|#|-|\+|\\\\|\/|_|:|~|@|\?|>|\*|\$|\%[0-9]{2}|[0-9])"
    regezz = re.compile(pattern)
    tokens = [token for token in regezz.findall(smile)]
    assert smile == ''.join(tokens), ("{} could not be joined".format(smile))
    return tokens

def encode_smiles(smile, max_len, char_dict):
    for _ in range(max_len - len(smile)):
        smile.append('_')
    smile_vec = [char_dict[c] for c in smile]
    return smile_vec

def get_char_weights(train_smiles, params, freq_penalty=0.5):
    char_dist = {}
    char_counts = np.zeros((params['NUM_CHAR'],))
    char_weights = np.zeros((params['NUM_CHAR'],))
    for k in params['CHAR_DICT'].keys():
        char_dist[k] = 0
    for smile in train_smiles:
        for i, char in enumerate(smile):
            char_dist[char] += 1
        for j in range(i, params['MAX_LENGTH']):
            char_dist['_'] += 1
    for i, v in enumerate(char_dist.values()):
        char_counts[i] = v
    top = np.sum(np.log(char_counts))
    for i in range(char_counts.shape[0]):
        char_weights[i] = top / np.log(char_counts[i])
    min_weight = char_weights.min()
    for i, w in enumerate(char_weights):
        if w > 2*min_weight:
            char_weights[i] = 2*min_weight
    scaler = MinMaxScaler([freq_penalty,1.0])
    char_weights = scaler.fit_transform(char_weights.reshape(-1, 1))
    return char_weights[:,0]


####### POSTPROCESSING HELPERS ##########

def decode_smiles(encoded_tensors, org_dict):
    smiles = []
    for i in range(encoded_tensors.shape[0]):
        encoded_tensor = encoded_tensors.cpu().numpy()[i,:] - 1
        smile = ''
        for i in range(encoded_tensor.shape[0]):
            idx = encoded_tensor[i]
            smile += org_dict[idx]
        smile = smile.replace('_', '')
        smiles.append(smile)
    return smiles

def calc_reconstruction_accuracies(input_smiles, output_smiles, max_len):
    smile_accs = []
    hits = 0
    misses = 0
    position_accs = np.zeros((2, max_len))
    for in_smi, out_smi in zip(input_smiles, output_smiles):
        if in_smi == out_smi:
            smile_accs.append(1)
        else:
            smile_accs.append(0)

        misses += abs(len(in_smi) - len(out_smi))
        for j, (token_in, token_out) in enumerate(zip(in_smi, out_smi)):
            if token_in == token_out:
                hits += 1
                position_accs[0,j] += 1
            else:
                misses += 1
            position_accs[1,j] += 1

    smile_acc = np.mean(smile_accs)
    token_acc = hits / (hits + misses)
    position_acc = []
    for i in range(position_accs.shape[1]):
        position_acc.append(position_accs[0,i] / position_accs[1,i])
    return smile_acc, token_acc, position_acc

def greedy_decode(model, src, src_mask, max_len, start_symbol):
    mem_key, mem_val, mu, logvar = model.encode(src, src_mask)
    ys = torch.ones(1,1).fill_(start_symbol).type_as(src.data)
    for i in range(max_len-1):
        out = model.decode(mem_key, mem_val, src_mask, Variable(ys),
                           Variable(subsequent_mask(ys.size(1)).type_as(src.data)))
        out = model.generator(out)
        prob = F.softmax(out[:, -1], dim=-1)
        _, next_word = torch.max(prob, dim=1)
        next_word = next_word.item() + 1
        ys = torch.cat([ys, torch.ones(1,1).type_as(src.data).fill_(next_word)], dim=1)
    return ys



####### GRADIENT TROUBLESHOOTING #########

def plot_grad_flow(named_parameters):
    ave_grads = []
    layers = []
    for n, p in named_parameters:
        if(p.requires_grad) and ("bias" not in n):
            layers.append(n)
            ave_grads.append(p.grad.abs().mean())
    layers = np.array(layers)
    ave_grads = np.array(ave_grads)
    fig = plt.figure(figsize=(12,6))
    plt.plot(ave_grads, alpha=0.3, color="b")
    plt.hlines(0, 0, len(ave_grads)+1, linewidth=1, color="k" )
    plt.xticks(range(0,len(ave_grads), 1), layers, rotation="vertical")
    plt.xlim(xmin=0, xmax=len(ave_grads))
    plt.ylim(ymin=0, ymax=5e-3)
    plt.xlabel("Layers")
    plt.ylabel("average gradient")
    plt.title("Gradient flow")
    plt.grid(True)
    plt.tight_layout()
    return plt

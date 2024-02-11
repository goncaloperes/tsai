# AUTOGENERATED! DO NOT EDIT! File to edit: ../../nbs/077_models.multimodal.ipynb.

# %% auto 0
__all__ = ['get_o_cont_idxs', 'get_feat_idxs', 'TensorSplitter', 'Embeddings', 'StaticBackbone', 'FusionMLP',
           'MultInputBackboneWrapper', 'MultInputWrapper']

# %% ../../nbs/077_models.multimodal.ipynb 3
import torch
import torch.nn as nn
import numpy as np
from collections import OrderedDict
from fastcore.test import test_eq
from fastcore.xtras import listify
from fastcore.xtras import L
from fastai.tabular.model import emb_sz_rule
from ..imports import default_device
from ..data.core import TSDataLoaders
from ..data.preprocessing import PatchEncoder
from ..learner import get_arch
from .utils import build_ts_model, output_size_calculator
from .layers import Reshape, LinBnDrop, get_act_fn, lin_nd_head, rocket_nd_head, GAP1d

# %% ../../nbs/077_models.multimodal.ipynb 4
def _to_list(idx):
    if idx is None:
        return []
    elif isinstance(idx, int):
        return [idx]
    elif isinstance(idx, list):
        return idx


def get_o_cont_idxs(c_in, s_cat_idxs=None, s_cont_idxs=None, o_cat_idxs=None):
    "Calculate the indices of the observed continuous features."
    all_features = np.arange(c_in).tolist()
    for idxs in [s_cat_idxs, s_cont_idxs, o_cat_idxs]:
        if idxs is not None:
            if not isinstance(idxs, list): idxs = [idxs]
            for idx in idxs:
                all_features.remove(idx)
    o_cont_idxs = all_features
    return o_cont_idxs


def get_feat_idxs(c_in, s_cat_idxs=None, s_cont_idxs=None, o_cat_idxs=None, o_cont_idxs=None):
    "Calculate the indices of the features used for training."
    idx_list = [s_cat_idxs, s_cont_idxs, o_cat_idxs, o_cont_idxs]
    s_cat_idxs, s_cont_idxs, o_cat_idxs, o_cont_idxs = list(map(_to_list, idx_list))
    if not o_cont_idxs:
        o_cont_idxs = get_o_cont_idxs(c_in, s_cat_idxs=s_cat_idxs, s_cont_idxs=s_cont_idxs, o_cat_idxs=o_cat_idxs)
    return s_cat_idxs, s_cont_idxs, o_cat_idxs, o_cont_idxs

# %% ../../nbs/077_models.multimodal.ipynb 6
class TensorSplitter(nn.Module):
    def __init__(self,
        s_cat_idxs:list=None, # list of indices for static categorical variables
        s_cont_idxs:list=None, # list of indices for static continuous variables
        o_cat_idxs:list=None, # list of indices for observed categorical variables
        o_cont_idxs:list=None, # list of indices for observed continuous variables
        k_cat_idxs:list=None, # list of indices for known categorical variables
        k_cont_idxs:list=None, # list of indices for known continuous variables
        horizon:int=None, # number of time steps to predict ahead
        ):
        super(TensorSplitter, self).__init__()
        assert s_cat_idxs or s_cont_idxs or o_cat_idxs or o_cont_idxs, "must specify at least one of s_cat_idxs, s_cont_idxs, o_cat_idxs, o_cont_idxs"
        if k_cat_idxs or k_cont_idxs:
            assert horizon is not None, "must specify horizon if using known variables"
        assert horizon is None or isinstance(horizon, int), "horizon must be an integer"
        self.s_cat_idxs = self._to_list(s_cat_idxs)
        self.s_cont_idxs = self._to_list(s_cont_idxs)
        self.o_cat_idxs = self._to_list(o_cat_idxs)
        self.o_cont_idxs = self._to_list(o_cont_idxs)
        self.k_cat_idxs = self._to_list(k_cat_idxs)
        self.k_cont_idxs = self._to_list(k_cont_idxs)
        idx_list = [self.s_cat_idxs, self.s_cont_idxs, self.o_cat_idxs, self.o_cont_idxs]
        if horizon:
            idx_list += [self.k_cat_idxs, self.k_cont_idxs]
        self.idx_list = list(map(self._to_list, idx_list))
        self._check_overlap()
        self.horizon = horizon

    def _check_overlap(self):
        indices = []
        for idx in self.idx_list:
            indices += idx
        if len(indices) != len(set(indices)):
            raise ValueError("Indices must not overlap between s_cat_idxs, s_cont_idxs, o_cat_idxs, and o_cont_idxs")

    @staticmethod
    def _to_list(idx):
        if idx is None:
            return []
        elif isinstance(idx, int):
            return [idx]
        elif isinstance(idx, list):
            return idx

    def forward(self, input_tensor):
        slices = []
        for idx, idxs in enumerate(self.idx_list):
        # for idx, idxs in enumerate([self.s_cat_idxs, self.s_cont_idxs, self.o_cat_idxs, self.o_cont_idxs, self.k_cat_idxs, self.k_cont_idxs]):
            if idxs:
                if idx < 2:  # s_cat_idxs or s_cont_idxs
                    slices.append(input_tensor[:, idxs, 0].long())
                elif idx < 4 and self.horizon is not None:  # o_cat_idxs or o_cont_idxs and horizon is not None
                    slices.append(input_tensor[:, idxs, :-self.horizon])
                else:  # k_cat_idxs or k_cont_idxs or o_cat_idxs or o_cont_idxs and horizon is None
                    slices.append(input_tensor[:, idxs, :])
            else:
                if idx < 2:  # s_cat_idxs or s_cont_idxs
                    slices.append(torch.empty((input_tensor.size(0), 0), device=input_tensor.device))  # return 2D empty tensor
                elif idx < 4 and self.horizon is not None: # o_cat_idxs or o_cont_idxs and horizon is not None
                        slices.append(torch.empty((input_tensor.size(0), 0, input_tensor.size(2)-self.horizon), device=input_tensor.device))
                else:   # k_cat_idxs or k_cont_idxs or o_cat_idxs or o_cont_idxs and horizon is None
                    slices.append(torch.empty((input_tensor.size(0), 0, input_tensor.size(2)), device=input_tensor.device))
        return slices


# %% ../../nbs/077_models.multimodal.ipynb 9
class Embeddings(nn.Module):
    "Embedding layers for each categorical variable in a 2D or 3D tensor"
    def __init__(self,
        n_embeddings:list, # List of num_embeddings for each categorical variable
        embedding_dims:list=None, # List of embedding dimensions for each categorical variable
        padding_idx:int=0, # Embedding padding_idx
        embed_dropout:float=0., # Dropout probability for `Embedding` layer
        **kwargs
        ):
        super().__init__()
        if not isinstance(n_embeddings, list): n_embeddings = [n_embeddings]
        if embedding_dims is None:
            embedding_dims = [emb_sz_rule(s) for s in n_embeddings]
        if not isinstance(embedding_dims, list): embedding_dims = [embedding_dims]
        embedding_dims = [emb_sz_rule(s) if s is None else s for s in n_embeddings]
        assert len(n_embeddings) == len(embedding_dims)
        self.embedding_dims = sum(embedding_dims)
        self.embedding_layers = nn.ModuleList([nn.Sequential(nn.Embedding(n,d,padding_idx=padding_idx, **kwargs),
                                                             nn.Dropout(embed_dropout)) for n,d in zip(n_embeddings, embedding_dims)])

    def forward(self, x):
        if x.ndim == 2:
            return torch.cat([e(x[:,i].long()) for i,e in enumerate(self.embedding_layers)],1)
        elif x.ndim == 3:
            return torch.cat([e(x[:,i].long()).transpose(1,2) for i,e in enumerate(self.embedding_layers)],1)

# %% ../../nbs/077_models.multimodal.ipynb 13
class StaticBackbone(nn.Module):
    "Static backbone model to embed static features"
    def __init__(self, c_in, c_out, seq_len, d=None, layers=[200, 100], dropouts=[0.1, 0.2], act=nn.ReLU(inplace=True), use_bn=False, lin_first=False):
        super().__init__()
        layers, dropouts = L(layers), L(dropouts)
        if len(dropouts) <= 1: dropouts = dropouts * len(layers)
        assert len(layers) == len(dropouts), '#layers and #dropout must match'
        self.flatten = Reshape()
        nf = [c_in * seq_len] + layers
        self.mlp = nn.ModuleList()
        for i in range(len(layers)): self.mlp.append(LinBnDrop(nf[i], nf[i+1], bn=use_bn, p=dropouts[i], act=get_act_fn(act), lin_first=lin_first))
        self.head_nf = nf[-1]

    def forward(self, x):
        x = self.flatten(x)
        for mlp in self.mlp: x = mlp(x)
        return x

# %% ../../nbs/077_models.multimodal.ipynb 18
class FusionMLP(nn.Module):
    def __init__(self, comb_dim, layers, act='relu', dropout=0., use_bn=True):
        super().__init__()
        self.avg_pool = GAP1d(1)
        layers = listify(layers)
        if not isinstance(dropout, list): dropout = [dropout]
        if len(dropout) != len(layers): dropout = dropout * len(layers)
        l = []
        for i,s in enumerate(layers):
            if use_bn: l.append(nn.BatchNorm1d(comb_dim if i == 0 else prev_s))
            if dropout[i]: l.append(nn.Dropout(dropout[i]))
            l.append(nn.Linear(comb_dim if i == 0 else prev_s, s))
            if act: l.append(get_act_fn(act))
            prev_s = s
        if l:
            self.mlp = nn.Sequential(*l)
        else:
            self.mlp = nn.Identity()

    def forward(self, x_cat, x_cont, x_emb):
        if x_emb.ndim == 3:
            x_emb = self.avg_pool(x_emb)
        output = torch.cat([x_cat, x_cont, x_emb], 1)
        output = self.mlp(output)
        return output

# %% ../../nbs/077_models.multimodal.ipynb 21
class MultInputBackboneWrapper(nn.Module):
    "Model backbone wrapper for input tensors with static and/ or observed, categorical and/ or numerical features."

    def __init__(self,
        arch,
        c_in:int=None, # number of input variables
        seq_len:int=None, # input sequence length
        d:tuple=None, # shape of the output tensor
        dls:TSDataLoaders=None, # TSDataLoaders object
        s_cat_idxs:list=None, # list of indices for static categorical variables
        s_cat_embeddings:list=None, # list of num_embeddings for each static categorical variable
        s_cat_embedding_dims:list=None, # list of embedding dimensions for each static categorical variable
        s_cont_idxs:list=None, # list of indices for static continuous variables
        o_cat_idxs:list=None, # list of indices for observed categorical variables
        o_cat_embeddings:list=None, # list of num_embeddings for each observed categorical variable
        o_cat_embedding_dims:list=None, # list of embedding dimensions for each observed categorical variable
        o_cont_idxs:list=None, # list of indices for observed continuous variables. All features not in s_cat_idxs, s_cont_idxs, o_cat_idxs are considered observed continuous variables.
        patch_len:int=None, # Number of time steps in each patch.
        patch_stride:int=None, # Stride of the patch.
        fusion_layers:list=[128], # list of layer dimensions for the fusion MLP
        fusion_act:str='relu', # activation function for the fusion MLP
        fusion_dropout:float=0., # dropout probability for the fusion MLP
        fusion_use_bn:bool=True, # boolean indicating whether to use batch normalization in the fusion MLP
        **kwargs
    ):
        super().__init__()

        # attributes
        c_in = c_in or dls.vars
        seq_len = seq_len or dls.len
        d = d or (dls.d if dls is not None else None)
        self.c_in, self.seq_len, self.d = c_in, seq_len, d

        # tensor splitter
        if o_cont_idxs is None:
            o_cont_idxs = get_o_cont_idxs(c_in, s_cat_idxs=s_cat_idxs, s_cont_idxs=s_cont_idxs, o_cat_idxs=o_cat_idxs)
        self.splitter = TensorSplitter(s_cat_idxs, s_cont_idxs, o_cat_idxs, o_cont_idxs)
        s_cat_idxs, s_cont_idxs, o_cat_idxs, o_cont_idxs = self.splitter.s_cat_idxs, self.splitter.s_cont_idxs, self.splitter.o_cat_idxs, self.splitter.o_cont_idxs
        assert c_in == sum([len(s_cat_idxs), len(s_cont_idxs), len(o_cat_idxs), len(o_cont_idxs)])

        # embeddings
        self.s_embeddings = Embeddings(s_cat_embeddings, s_cat_embedding_dims) if s_cat_idxs else nn.Identity()
        self.o_embeddings = Embeddings(o_cat_embeddings, o_cat_embedding_dims) if o_cat_idxs else nn.Identity()

        # patch encoder
        if patch_len is not None:
            patch_stride = patch_stride or patch_len
            self.patch_encoder = PatchEncoder(patch_len, patch_stride, seq_len=seq_len)
            c_mult = patch_len
            seq_len = (seq_len + self.patch_encoder.pad_size - patch_len) // patch_stride + 1
        else:
            self.patch_encoder = nn.Identity()
            c_mult = 1

        # backbone
        n_s_features = len(s_cont_idxs) + (self.s_embeddings.embedding_dims if s_cat_idxs else 0)
        n_o_features = (len(o_cont_idxs) + (self.o_embeddings.embedding_dims if o_cat_idxs else 0)) * c_mult
        if isinstance(arch, str):
            arch = get_arch(arch)
        if isinstance(arch, nn.Module):
            o_model = arch
        else:
            o_model = build_ts_model(arch, c_in=n_o_features, c_out=1, seq_len=seq_len, d=d, **kwargs)
        assert hasattr(o_model, "backbone"), "the selected arch must have a backbone"
        o_backbone = getattr(o_model, "backbone")
        self.o_backbone = o_backbone
        backbone_features = output_size_calculator(o_backbone, n_o_features, seq_len)[0]

        # fusion layer
        fusion_layers = listify(fusion_layers)
        self.fusion_layer = FusionMLP(n_s_features + backbone_features, layers=fusion_layers, act=fusion_act, dropout=fusion_dropout, use_bn=fusion_use_bn)
        self.head_nf = fusion_layers[-1]


    def forward(self, x):
        # split x into static cat, static cont, observed cat, and observed cont
        s_cat, s_cont, o_cat, o_cont = self.splitter(x)

        # create categorical embeddings
        s_cat = self.s_embeddings(s_cat)
        o_cat = self.o_embeddings(o_cat)

        # contatenate observed features
        o_x = torch.cat([o_cat, o_cont], 1)

        # patch encoder
        o_x = self.patch_encoder(o_x)

        # pass static and observed features through their respective backbones
        o_x = self.o_backbone(o_x)

        # fusion layer
        x = self.fusion_layer(s_cat, s_cont, o_x)

        return x

# %% ../../nbs/077_models.multimodal.ipynb 22
class MultInputWrapper(nn.Sequential):
    def __init__(self,
        arch,
        c_in:int=None, # number of input variables
        c_out:int=1, # number of output variables
        seq_len:int=None, # input sequence length
        d:tuple=None, # shape of the output tensor
        dls:TSDataLoaders=None, # TSDataLoaders object
        s_cat_idxs:list=None, # list of indices for static categorical variables
        s_cat_embeddings:list=None, # list of num_embeddings for each static categorical variable
        s_cat_embedding_dims:list=None, # list of embedding dimensions for each static categorical variable
        s_cont_idxs:list=None, # list of indices for static continuous variables
        o_cat_idxs:list=None, # list of indices for observed categorical variables
        o_cat_embeddings:list=None, # list of num_embeddings for each observed categorical variable
        o_cat_embedding_dims:list=None, # list of embedding dimensions for each observed categorical variable
        o_cont_idxs:list=None, # list of indices for observed continuous variables. All features not in s_cat_idxs, s_cont_idxs, o_cat_idxs are considered observed continuous variables.
        patch_len:int=None, # Number of time steps in each patch.
        patch_stride:int=None, # Stride of the patch.
        fusion_layers:list=128, # list of layer dimensions for the fusion MLP
        fusion_act:str='relu', # activation function for the fusion MLP
        fusion_dropout:float=0., # dropout probability for the fusion MLP
        fusion_use_bn:bool=True, # boolean indicating whether to use batch normalization in the fusion MLP
        custom_head=None, # custom head to replace the default head
        **kwargs
    ):

        # create backbone
        backbone = MultInputBackboneWrapper(arch, c_in=c_in, seq_len=seq_len, d=d, dls=dls, s_cat_idxs=s_cat_idxs, s_cat_embeddings=s_cat_embeddings, s_cat_embedding_dims=s_cat_embedding_dims,
                                            s_cont_idxs=s_cont_idxs, o_cat_idxs=o_cat_idxs, o_cat_embeddings=o_cat_embeddings, o_cat_embedding_dims=o_cat_embedding_dims, o_cont_idxs=o_cont_idxs,
                                            patch_len=patch_len, patch_stride=patch_stride, fusion_layers=fusion_layers, fusion_act=fusion_act, fusion_dropout=fusion_dropout, fusion_use_bn=fusion_use_bn, **kwargs)

        # create head
        self.head_nf = backbone.head_nf
        self.c_out = c_out
        self.seq_len = seq_len
        if custom_head:
            if isinstance(custom_head, nn.Module): head = custom_head
            else: head = custom_head(self.head_nf, c_out, seq_len, d=d)
        else:
            head = nn.Linear(self.head_nf, c_out)
        super().__init__(OrderedDict([('backbone', backbone), ('head', head)]))


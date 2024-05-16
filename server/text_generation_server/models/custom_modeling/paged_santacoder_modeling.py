import torch
import torch.distributed

from torch import nn
from transformers.activations import ACT2FN
from typing import Optional

from fms_extras.utils.cache.paged import PagedAttentionCacheData, PagedAttentionCacheDataLayer
from text_generation_server.utils.flash_attn import attention
from text_generation_server.utils.layers import (
    TensorParallelRowLinear,
    TensorParallelColumnLinear,
    TensorParallelHead,
    TensorParallelEmbedding,
    FastLayerNorm,
    get_linear,
)


def load_multi_mqa(
        config, prefix: str, weights, bias: bool, head_size, num_heads, hidden_size
):
    return (_load_multi_mqa_gptq if config.quantize == "gptq" else _load_multi_mqa)(
        config, prefix, weights, bias, head_size, num_heads, hidden_size
    )


def _load_multi_mqa_gptq(
        config, prefix: str, weights, bias: bool, head_size, num_heads, hidden_size
):
    if any("c_attn" in k for k in weights.routing.keys()) and not config.transpose:
        world_size = weights.process_group.size()
        rank = weights.process_group.rank()

        slice_ = weights._get_slice(f"{prefix}.c_attn.qweight")
        shape = slice_.get_shape()
        block_size = (shape[1] - 2 * head_size) // world_size
        start = rank * block_size
        stop = (rank + 1) * block_size
        assert (shape[1] - 2 * head_size) % world_size == 0
        q_tensor = slice_[:, start:stop]
        kv_tensor = slice_[:, -2 * head_size:]
        qweight = torch.cat([q_tensor, kv_tensor], dim=1)
        qweight = qweight.to(device=weights.device)

        slice_ = weights._get_slice(f"{prefix}.c_attn.scales")
        shape = slice_.get_shape()
        block_size = (shape[1] - 2 * head_size) // world_size
        start = rank * block_size
        stop = (rank + 1) * block_size
        assert (shape[1] - 2 * head_size) % world_size == 0
        q_tensor = slice_[:, start:stop]
        kv_tensor = slice_[:, -2 * head_size:]
        scales = torch.cat([q_tensor, kv_tensor], dim=1)
        scales = scales.to(device=weights.device)

        slice_ = weights._get_slice(f"{prefix}.c_attn.qzeros")
        shape = slice_.get_shape()
        block_size = (shape[1] - (2 * head_size) * 4 // 32) // world_size
        start = rank * block_size
        stop = (rank + 1) * block_size
        assert 2 * head_size % (32 // 4) == 0
        q_tensor = slice_[:, start:stop]
        kv_tensor = slice_[:, -2 * head_size * 4 // 32:]
        qzeros = torch.cat([q_tensor, kv_tensor], dim=1)
        qzeros = qzeros.to(device=weights.device)

        g_idx = weights.get_tensor(f"{prefix}.c_attn.g_idx")
        g_idx = g_idx.to(device=weights.device)
        bits, groupsize = weights._get_gptq_params()

        from text_generation_server.utils.layers import HAS_EXLLAMA
        weight = (qweight, qzeros, scales, g_idx, bits, groupsize, HAS_EXLLAMA)

        if bias:
            slice_ = weights._get_slice(f"{prefix}.c_attn.bias")
            shape = slice_.get_shape()
            block_size = (shape[0] - 2 * head_size) // world_size
            assert (shape[0] - 2 * head_size) % world_size == 0
            # q_tensor = slice_[start:stop]
            start = rank * block_size
            stop = (rank + 1) * block_size
            q_tensor = slice_[start:stop]
            kv_tensor = slice_[-2 * head_size:]
            bias = torch.cat([q_tensor, kv_tensor], dim=0)
            bias = bias.to(device=weights.device)

        return TensorParallelColumnLinear(get_linear(weight, bias, config.quantize))
    else:
        raise NotImplementedError("Gptq loading with santacoder is not implemented")


def _load_multi_mqa(
        config, prefix: str, weights, bias: bool, head_size, num_heads, hidden_size
):
    if any("c_attn" in k for k in weights.routing.keys()):
        slice_ = weights._get_slice(f"{prefix}.c_attn.weight")
        shape = slice_.get_shape()
        world_size = weights.process_group.size()
        rank = weights.process_group.rank()
        if config.transpose:
            block_size = (shape[1] - 2 * head_size) // world_size
            start = rank * block_size
            stop = (rank + 1) * block_size
            assert (shape[1] - 2 * head_size) % world_size == 0
            q_tensor = slice_[:, start:stop]
            kv_tensor = slice_[:, -2 * head_size:]
            weight = torch.cat([q_tensor, kv_tensor], dim=1).T
        else:
            block_size = (shape[0] - 2 * head_size) // world_size
            start = rank * block_size
            stop = (rank + 1) * block_size
            assert (shape[0] - 2 * head_size) % world_size == 0
            q_tensor = slice_[start:stop]
            kv_tensor = slice_[-2 * head_size:]
            weight = torch.cat([q_tensor, kv_tensor], dim=0)
        if bias:
            slice_ = weights._get_slice(f"{prefix}.c_attn.bias")
            shape = slice_.get_shape()
            block_size = (shape[0] - 2 * head_size) // world_size
            assert (shape[0] - 2 * head_size) % world_size == 0
            # q_tensor = slice_[start:stop]
            start = rank * block_size
            stop = (rank + 1) * block_size
            q_tensor = slice_[start:stop]
            kv_tensor = slice_[-2 * head_size:]
            bias = torch.cat([q_tensor, kv_tensor], dim=0)
    else:
        if config.transpose:
            w = [
                weights.get_sharded(f"{prefix}.q_attn.weight", dim=1).T,
                weights.get_tensor(f"{prefix}.kv_attn.weight").T,
            ]
            weight = torch.cat(w, dim=0)
        else:
            w = [
                weights.get_sharded(f"{prefix}.q_attn.weight", dim=0),
                weights.get_tensor(f"{prefix}.kv_attn.weight"),
            ]
            weight = torch.cat(w, dim=1)

        if bias:
            b = [
                weights.get_sharded(f"{prefix}.q_attn.bias", dim=0),
                weights.get_tensor(f"{prefix}.kv_attn.bias"),
            ]
            bias = torch.cat(b, dim=0)
        else:
            bias = None

    weight = weight.to(dtype=weights.dtype).to(device=weights.device)
    assert list(weight.shape) == [
        (num_heads + 2) * head_size,
        hidden_size,
    ], f"{weight.shape} != {[(num_heads + 2) * head_size, hidden_size]}"
    if bias is not None:
        bias = bias.to(dtype=weights.dtype).to(device=weights.device)
        assert list(bias.shape) == [
            (num_heads + 2) * head_size
        ], f"{weight.shape} != {[(num_heads + 2) * head_size]}"
    return TensorParallelColumnLinear(get_linear(weight, bias, config.quantize))


def load_col(config, prefix: str, weights, bias: bool):
    if config.transpose:
        weight = weights.get_sharded(f"{prefix}.weight", dim=1).T
    else:
        weight = weights.get_multi_weights_col(
            [prefix], quantize=config.quantize, dim=0
        )

    if bias:
        bias = weights.get_sharded(f"{prefix}.bias", dim=0)
    else:
        bias = None
    return TensorParallelColumnLinear(get_linear(weight, bias, config.quantize))


def load_row(config, prefix: str, weights, bias: bool):
    if config.transpose:
        weight = weights.get_sharded(f"{prefix}.weight", dim=0).T
    else:
        weight = weights.get_multi_weights_row(prefix, quantize=config.quantize)

    if bias and weights.process_group.rank() == 0:
        # Rank is only on the first rank process
        bias = weights.get_tensor(f"{prefix}.bias")
    else:
        bias = None
    return TensorParallelRowLinear(
        get_linear(weight, bias, config.quantize), process_group=weights.process_group
    )


class PagedMQAttention(torch.nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()
        num_heads = config.num_attention_heads
        hidden_size = config.hidden_size

        self.num_heads = num_heads
        self.hidden_size = hidden_size
        self.head_size = hidden_size // num_heads

        if self.num_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_heads` must be divisible by `num_shards` (got `num_heads`: {self.num_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        self.num_heads = self.num_heads // weights.process_group.size()

        self.softmax_scale = self.head_size ** (-0.5)

        self.c_attn = load_multi_mqa(
            config,
            prefix=prefix,
            weights=weights,
            bias=True,
            head_size=self.head_size,
            hidden_size=hidden_size,
            num_heads=self.num_heads,
        )
        self.c_proj = load_row(
            config, prefix=f"{prefix}.c_proj", weights=weights, bias=True
        )

    def forward(
            self,
            hidden_states,
            cache_data_layer: PagedAttentionCacheDataLayer,
    ):
        qkv = self.c_attn(hidden_states)

        # Split query from key_value
        query, key_value = qkv.split(
            [self.head_size * self.num_heads, 2 * self.head_size], dim=1
        )

        # Prepare query and key_value for indexing
        query = query.view(-1, self.num_heads, self.head_size)
        key_value = key_value.view(-1, 2, 1, self.head_size)
        key = torch.select(key_value, dim=1, index=0)
        value = torch.select(key_value, dim=1, index=1)

        key_after_store, value_after_store = cache_data_layer.store(key, value)

        # Prefill
        if not cache_data_layer.is_filled():

            # flash attention
            attn_output = attention(
                query,
                key,
                value,
                cache_data_layer.context_lengths.int(),
                cache_data_layer.max_sequence_length,
                self.softmax_scale,
            )
        # Decode
        else:
            attn_output = cache_data_layer.attend(query)

        return self.c_proj(attn_output.view(-1, self.num_heads * self.head_size))


class MLP(nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()
        act = config.activation_function
        self.act = (
            ACT2FN[act]
            if "gelu" not in act
            else lambda x: torch.nn.functional.gelu(
                x,
                approximate="tanh"
                if act in ["gelu_fast", "gelu_pytorch_tanh"]
                else "none",
            )
        )

        self.c_fc = load_col(
            config, prefix=f"{prefix}.c_fc", weights=weights, bias=True
        )
        self.c_proj = load_row(
            config, prefix=f"{prefix}.c_proj", weights=weights, bias=True
        )

    def forward(self, hidden_states):
        hidden_states = self.c_fc(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.c_proj(hidden_states)
        return hidden_states


class Block(nn.Module):
    def __init__(self, layer_id, config, weights):
        super().__init__()
        prefix = f"transformer.h.{layer_id}"
        self.ln_1 = FastLayerNorm.load(
            prefix=f"{prefix}.ln_1", weights=weights, eps=config.layer_norm_epsilon
        )
        self.ln_2 = FastLayerNorm.load(
            prefix=f"{prefix}.ln_2", weights=weights, eps=config.layer_norm_epsilon
        )
        self.attn = PagedMQAttention(
            prefix=f"{prefix}.attn",
            config=config,
            weights=weights,
        )
        self.mlp = MLP(
            prefix=f"{prefix}.mlp",
            config=config,
            weights=weights,
        )

    def forward(
        self,
        hidden_states,
        residual,
        cache_data_layer: PagedAttentionCacheDataLayer
    ):
        hidden_states, residual = self.ln_1(hidden_states, residual)
        hidden_states = self.attn(
            hidden_states,
            cache_data_layer,
        )

        hidden_states, residual = self.ln_2(hidden_states, residual)

        mlp_output = self.mlp(hidden_states)

        return mlp_output, residual


class PagedSantacoderModel(nn.Module):
    def __init__(self, config, weights):
        super().__init__()
        self.config = config

        self.process_group = weights.process_group
        self.wte = TensorParallelEmbedding(
            prefix="transformer.wte",
            weights=weights,
            reduce=False,
        )
        self.wpe = TensorParallelEmbedding(
            prefix="transformer.wpe",
            weights=weights,
            reduce=False,
        )

        self.h = nn.ModuleList(
            [
                Block(
                    layer_id,
                    config,
                    weights,
                )
                for layer_id in range(config.num_hidden_layers)
            ]
        )
        self.ln_f = FastLayerNorm.load(
            prefix="transformer.ln_f", weights=weights, eps=config.layer_norm_epsilon
        )

        self.head_size = self.h[0].attn.head_size
        self.num_heads = self.h[0].attn.num_heads

    def forward(
        self,
        input_ids,
        position_ids,
        cache_data: PagedAttentionCacheData,
        inputs_embeds: Optional[torch.Tensor] = None,
    ):
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time"
            )

        if inputs_embeds is not None:
            hidden_states = inputs_embeds + self.wpe(position_ids)
        else:
            hidden_states = self.wte(input_ids) + self.wpe(position_ids)

        if self.process_group.size() > 1:
            torch.distributed.all_reduce(hidden_states, group=self.process_group)

        residual = None
        for i, layer in enumerate(self.h):
            hidden_states, residual = layer(
                hidden_states,
                residual,
                cache_data.get_layer(i),
            )

        hidden_states, _ = self.ln_f(hidden_states, residual)

        return hidden_states


class PagedSantacoderForCausalLM(nn.Module):
    def __init__(self, config, weights):
        super().__init__()
        self.transformer = PagedSantacoderModel(config, weights)
        self.lm_head = TensorParallelHead.load(
            config, prefix="transformer.wte", weights=weights
        )

    def get_kv_cache_block_size(self, block_size: int) -> int:
        return block_size * self.model.head_size * 2

    def get_input_embeddings(self) -> nn.Module:
        return self.transformer.wte

    def forward(
        self,
        input_ids,
        position_ids,
        cache_data: Optional[PagedAttentionCacheData],
        inputs_embeds: Optional[torch.Tensor] = None,
        return_embeds: bool = False,
    ):
        hidden_states = self.transformer(
            input_ids,
            position_ids,
            cache_data,
            inputs_embeds,
        )
        logits = self.lm_head(hidden_states)
        if return_embeds:
            return logits, hidden_states
        else:
            return logits

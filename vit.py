from turtle import forward
import torch 
import torch.nn as nn
from einops import rearrange
from einops.layers.torch import Rearrange
import math 
import warnings
torch.autograd.set_detect_anomaly(True)
def pair(t):
    return t if isinstance(t, tuple) else (t, t)

def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)


def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor
class Attention(nn.Module):
    def __init__(self, dim, heads, dim_head, dropout, qkv_bias=False) -> None:
        super(Attention, self).__init__()
        inner_dim = dim_head *  heads   
        self.heads = heads
        self.scale = dim_head ** -0.5 # 1/sqrt(dim_head)
        self.dropout = nn.Dropout(dropout)
        self.to_qkv = nn.Linear(dim, inner_dim*3, bias=qkv_bias) # One linear for all Q, K, V for all heads
        self.softmax = nn.Softmax(dim=-1) # Softmax over num_patches for each head separately
        self.to_out = nn.Sequential(nn.Linear(inner_dim, dim), nn.Dropout(dropout))
    
    def forward(self, x) -> torch.Tensor:
        qkv = self.to_qkv(x).chunk(3, dim=-1) # Split Q, K, V for all heads, (batch, num_patches, dim) -> 3x(batch, num_patches, dim_head*heads)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.heads), qkv) # Rearrange to (batch, heads, num_patches, dim_head)
        prod = torch.einsum('b h n d, b h m d -> b h n m', q, k) * self.scale # (batch, heads, num_patches, num_patches)
        prod = self.softmax(prod)
        prod = self.dropout(prod)
        out = torch.einsum('b h n m, b h m d -> b h n d', prod, v) # (batch, heads, num_patches, dim_head)
        out = rearrange(out, 'b h n d -> b n (h d)', h=self.heads) # (batch, num_patches, dim_head*heads)
        return self.to_out(out) #(batch, num_patches, dim)

class TransformerBlock(nn.Module):
    def __init__(self, dim, heads, dim_head, mlp_dim, dropout, qkv_bias=False, norm_layer=nn.LayerNorm) -> None:
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attention = Attention(dim, heads, dim_head, dropout, qkv_bias=qkv_bias)
        self.mlp = nn.Sequential(
            nn.Linear(dim, mlp_dim),
            nn.Dropout(dropout),
            nn.GELU(),
            nn.Linear(mlp_dim, dim),
            nn.Dropout(dropout)
        )
        self.norm2 = norm_layer(dim)
    
    def forward(self, x) -> torch.Tensor:
        x = self.norm1(x)
        x = x + self.attention(x)
        x = self.norm2(x)
        x = x + self.mlp(x)
        return x

class ConvProjection(nn.Module):
    def __init__(self, channels, dim, patch_size) -> None:
        super(ConvProjection, self).__init__()
        self.projection = nn.Conv2d(channels, dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x) -> torch.Tensor:
        x = self.projection(x)
        return x.flatten(2).transpose(1,2)

class ViT(nn.Module):
    """
    Visual Transformer
    """
    def __init__(self,
                image_size,
                patch_size,
                channels,
                dim,
                depth,
                heads,
                mlp_dim,
                dim_head=64, 
                pool=False, 
                projection='linear', # if not linear use a convolution instead (in original paper they use linear)
                dropout=0.,
                emb_dropout = 0.,
                qkv_bias = False,
                norm_layer = nn.LayerNorm,
                ) -> None:
        super(ViT, self).__init__()
        image_height, image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)
        num_patches = (image_height // patch_height) * (image_width // patch_width)
        patch_dim =  patch_height * patch_width * channels
        self.patch_height = patch_height
        self.patch_width = patch_width

        self.patch_to_embedding = nn.Sequential(
            # Rearrange from (batch, channels, H, W) to (batch, num_patches, patch_dim) num_patches = channels * (H//patch_height) * (W//patch_width)
            Rearrange('b c (h ph) (w pw) -> b (h w) (ph pw c)', ph=patch_height, pw=patch_width), # Let Rearrange figure out h and w which is num_patches
            nn.Linear(patch_dim, dim)) if projection=='linear' else ConvProjection(channels, dim, patch_size)
            
        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 1, dim)) # Trainable parameter Add 1 for cls token
        self.cls_token = nn.Parameter(torch.randn(1, 1, dim)) # Trainable parameter for the class token, refers to the task at hand used for training the transformer.
        self.dropout = nn.Dropout(emb_dropout)
        self.layers = nn.ModuleList([TransformerBlock(dim, heads, dim_head, mlp_dim, dropout, qkv_bias=qkv_bias, norm_layer=norm_layer) for _ in range(depth)])
        self.pool = pool
        self.norm = norm_layer(dim)
    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
    def interpolate_pos_encoding(self, x, w, h) -> torch.Tensor:
        """
        Interpolate pos encoding in transfer learning or for images with different size
        function taken from https://github.com/facebookresearch/dino/
        """
        num_patches = x.shape[1] - 1
        N = self.pos_embedding.shape[1] - 1
        if num_patches == N and w == h: # if size matches, do nothing
            return self.pos_embedding
        class_pos_embed = self.pos_embedding[:, 0]
        patch_pos_embed = self.pos_embedding[:, 1:]
        dim = x.shape[-1]
        h0 = h // self.patch_height
        w0 = w // self.patch_width
        
        h0, w0 = h0 + 0.1, w0 + 0.1
        patch_pos_embed = nn.functional.interpolate(
            patch_pos_embed.reshape(1, int(math.sqrt(N)), int(math.sqrt(N)), dim).permute(0, 3, 1, 2),
            scale_factor=(h0 / math.sqrt(N), w0 / math.sqrt(N)),
            mode='bicubic',
        )
        assert int(h0) == patch_pos_embed.shape[-2] and int(w0) == patch_pos_embed.shape[-1]
        patch_pos_embed = patch_pos_embed.permute(0, 2, 3, 1).view(1, -1, dim)
        return torch.cat((class_pos_embed.unsqueeze(0), patch_pos_embed), dim=1)

    def forward(self, x, norm=True, mixup=None, lbda = None, perm = None) -> torch.Tensor:
        b, _, w, h = x.shape
        x = self.patch_to_embedding(x)# Project to embedding space (batch, num_patches, dim)
        n = x.shape[1]
        cls_tokens = self.cls_token.expand(b, -1, -1) # Expand cls_token to (batch, 1, dim)
        x = torch.cat((cls_tokens, x), dim=1) # Concat cls_token to (batch, num_patches+1, dim)
        x += self.interpolate_pos_encoding(x, w, h) # Add positional embedding, make sure to add only num_patches+1 embeddings in case of variable image size #self.pos_embedding[:, :(n+1)]
        x = self.dropout(x)
        for layer in self.layers: # Pass through transformer layers
            x = layer(x)
        if norm: x = self.norm(x)
        features = x.mean(dim=1) if self.pool else x[:,0]# Average pooling for features
        return features

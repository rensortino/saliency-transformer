from math import log2, sqrt
from einops import rearrange
from torch import nn, einsum
import torch.nn.functional as F

import torch

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def eval_decorator(fn):
    def inner(model, *args, **kwargs):
        was_training = model.training
        model.eval()
        out = fn(model, *args, **kwargs)
        model.train(was_training)
        return out
    return inner

class ResBlock(nn.Module):
    def __init__(self, chan):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(chan, chan, 3, padding = 1),
            nn.ReLU(),
            nn.Conv2d(chan, chan, 3, padding = 1),
            nn.ReLU(),
            nn.Conv2d(chan, chan, 1)
        )

    def forward(self, x):
        return self.net(x) + x

class DiscreteVAE(nn.Module):
    def __init__(
        self,
        image_size = 64,
        num_tokens = 256,
        codebook_dim = 256,
        num_layers = 2,
        num_resnet_blocks = 2,
        hidden_dim = 256,
        channels = 3,
        smooth_l1_loss = False,
        temperature = 0.9,
        straight_through = False,
        kl_div_loss_weight = 0.,
        normalization = ((*((0.5,) * 3), 0), (*((0.5,) * 3), 1))
    ):
        super().__init__()
        assert log2(image_size).is_integer(), 'image size must be a power of 2'
        assert num_layers >= 1, 'number of layers must be greater than or equal to 1'
        has_resblocks = num_resnet_blocks > 0

        self.channels = channels
        self.image_size = image_size
        self.num_tokens = num_tokens
        self.num_layers = num_layers
        self.temperature = temperature
        self.straight_through = straight_through
        self.codebook = nn.Embedding(num_tokens, codebook_dim)

        hdim = hidden_dim

        enc_chans = [hidden_dim] * num_layers
        dec_chans = list(reversed(enc_chans))

        enc_chans = [channels, *enc_chans]

        dec_init_chan = codebook_dim if not has_resblocks else dec_chans[0]
        dec_chans = [dec_init_chan, *dec_chans]

        enc_chans_io, dec_chans_io = map(lambda t: list(zip(t[:-1], t[1:])), (enc_chans, dec_chans))

        enc_layers = []
        dec_layers = []

        for (enc_in, enc_out), (dec_in, dec_out) in zip(enc_chans_io, dec_chans_io):
            enc_layers.append(nn.Sequential(nn.Conv2d(enc_in, enc_out, 4, stride = 2, padding = 1), nn.ReLU()))
            dec_layers.append(nn.Sequential(nn.ConvTranspose2d(dec_in, dec_out, 4, stride = 2, padding = 1), nn.ReLU()))

        for _ in range(num_resnet_blocks):
            dec_layers.insert(0, ResBlock(dec_chans[1]))
            enc_layers.append(ResBlock(enc_chans[-1]))

        if num_resnet_blocks > 0:
            dec_layers.insert(0, nn.Conv2d(codebook_dim, dec_chans[1], 1))

        enc_layers.append(nn.Conv2d(enc_chans[-1], num_tokens, 1))
        dec_layers.append(nn.Conv2d(dec_chans[-1], channels, 1))

        self.encoder = nn.Sequential(*enc_layers)
        self.decoder = nn.Sequential(*dec_layers)

        self.loss_fn = F.smooth_l1_loss if smooth_l1_loss else F.mse_loss
        self.kl_div_loss_weight = kl_div_loss_weight

        # take care of normalization within class
        self.normalization = tuple(map(lambda t: t[:channels], normalization))

    def norm(self, images):
        if not exists(self.normalization):
            return images

        means, stds = map(lambda t: torch.as_tensor(t).to(images), self.normalization)
        means, stds = map(lambda t: rearrange(t, 'c -> () c () ()'), (means, stds))
        images = images.clone()
        images.sub_(means).div_(stds)
        return images

    @torch.no_grad()
    @eval_decorator
    def encode(self, images):
        logits = self(images, return_logits = True)
        codebook_indices = logits.argmax(dim = 1)#.flatten(1)
        return codebook_indices

    def decode(
        self,
        img_seq
    ):
        image_embeds = self.codebook(img_seq)
        # b, n, d = image_embeds.shape
        # h = w = int(sqrt(n))

        # image_embeds = rearrange(image_embeds, 'b (h w) d -> b d h w', h = h, w = w)
        image_embeds = rearrange(image_embeds, 'b h w d -> b d h w')
        images = self.decoder(image_embeds)
        return images

    def forward(
        self,
        img,
        return_loss = False,
        return_recons = False,
        return_logits = False,
        temp = None
    ):
        device, num_tokens, image_size, kl_div_loss_weight = img.device, self.num_tokens, self.image_size, self.kl_div_loss_weight
        assert img.shape[-1] == image_size and img.shape[-2] == image_size, f'input must have the correct image size {image_size}'

        img = self.norm(img)

        logits = self.encoder(img)

        if return_logits:
            return logits # return logits for getting hard image indices for DALL-E training

        temp = default(temp, self.temperature)
        soft_one_hot = F.gumbel_softmax(logits, tau = temp, dim = 1, hard = self.straight_through)
        sampled = einsum('b n h w, n d -> b d h w', soft_one_hot, self.codebook.weight)
        out = self.decoder(sampled)

        if not return_loss:
            return out

        # reconstruction loss

        recon_loss = self.loss_fn(img, out)

        # kl divergence

        logits = rearrange(logits, 'b n h w -> b (h w) n')
        log_qy = F.log_softmax(logits, dim = -1)
        log_uniform = torch.log(torch.tensor([1. / num_tokens], device = device))
        kl_div = F.kl_div(log_uniform, log_qy, None, None, 'batchmean', log_target = True)

        loss = recon_loss + (kl_div * kl_div_loss_weight)

        if not return_recons:
            return loss

        return loss, out
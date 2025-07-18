import torch
import torch.nn as nn
import torch.nn.functional as F
import warnings
from typing import Dict, List, Optional, Tuple, Union, Any


class AdaptiveBiasReflectiveLayerV7(nn.Module):
    """
    Adaptive Bias Reflective Layer with multi-scale projection and KL-based correction mechanism.
    
    This layer performs normalization with learnable parameters and applies bias corrections
    based on KL divergence from a reference distribution.
    
    Args:
        hidden_dim (int): Dimension of input features
        ref_dim (int, optional): Dimension of reference space. Defaults to 64.
        alpha (float, optional): Learning rate for corrections. Defaults to 0.01.
        eps (float, optional): Small constant for numerical stability. Defaults to 1e-6.
        kl_threshold (float, optional): Threshold for KL divergence to trigger corrections. Defaults to 0.1.
        ema_decay (float, optional): Decay factor for exponential moving averages. Defaults to 0.95.
        scales (List[float], optional): Scale factors for multi-scale projections. Defaults to [1.0, 0.5, 0.1].
        max_corrections (int, optional): Maximum number of corrections to apply. Defaults to 3.
        compression_factor (int, optional): Factor for quantizing corrections. Defaults to 4.
        trainable_reference (bool, optional): Whether reference distribution is trainable. Defaults to False.
        monitor_only (bool, optional): If True, only monitor without applying corrections. Defaults to False.
    """
    def __init__(self, 
                 hidden_dim: int, 
                 ref_dim: int = 64, 
                 alpha: float = 0.01, 
                 eps: float = 1e-6,
                 kl_threshold: float = 0.1, 
                 ema_decay: float = 0.95, 
                 scales: List[float] = [1.0, 0.5, 0.1],
                 max_corrections: int = 3, 
                 compression_factor: int = 4,
                 trainable_reference: bool = False, 
                 monitor_only: bool = False,
                 gradient_clip_value: Optional[float] = None):
        super().__init__()
        
        # Validate inputs
        if hidden_dim <= 0 or ref_dim <= 0:
            raise ValueError("Dimensions must be positive integers")
        if not (0 < alpha < 1):
            warnings.warn(f"Alpha value {alpha} is outside recommended range (0, 1)")
        if eps <= 0:
            raise ValueError("Epsilon must be positive")
            
        self.hidden_dim = hidden_dim
        self.ref_dim = ref_dim
        self.alpha = alpha
        self.eps = eps
        self.kl_threshold = kl_threshold
        self.ema_decay = ema_decay
        self.scales = scales
        self.max_corrections = max_corrections
        self.compression_factor = compression_factor
        self.monitor_only = monitor_only
        self.gradient_clip_value = gradient_clip_value

        # Normalization learnables
        self.gamma = nn.Parameter(torch.ones(hidden_dim))
        self.beta = nn.Parameter(torch.zeros(hidden_dim))

        # Projection weights with improved initialization
        proj_std = 1.0 / (hidden_dim ** 0.5)
        self.proj = nn.Parameter(torch.randn(ref_dim, hidden_dim) * proj_std)
        self.proj_bias = nn.Parameter(torch.zeros(ref_dim))
        self.proj_weights = nn.Parameter(torch.ones(len(scales), ref_dim))
        self.proj_sparsity = 0.01

        # Reference distribution
        self.ref_mu = nn.Parameter(torch.zeros(ref_dim), requires_grad=trainable_reference)
        self.ref_sigma = nn.Parameter(torch.ones(ref_dim), requires_grad=trainable_reference)

        # Tracking
        self.register_buffer("kl_ema", torch.tensor(0.0))
        self.register_buffer("variance_ema", torch.tensor(1.0))
        self.register_buffer("info_loss_ema", torch.tensor(0.0))
        self.register_buffer("correction_buffer", torch.zeros(hidden_dim))  # Initialize with proper shape
        self.register_buffer("compressed_history", None)
        
        # Performance optimizations
        self._last_batch_size = 0
        self._cached_ref_mu = None
        self._cached_ref_sigma = None

    def _project(self, x: torch.Tensor, scale_idx: int, scale: float) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Project input tensor to reference space using scale-specific weights.
        
        Args:
            x: Input tensor of shape [batch_size, seq_len, hidden_dim]
            scale_idx: Index into self.scales
            scale: Scale factor for this projection
            
        Returns:
            Tuple of (projected tensor, weighted projection matrix)
        """
        # Apply sigmoid for stability and interpretability
        weights = torch.sigmoid(self.proj_weights[scale_idx])
        
        # Apply weights to projection matrix
        weighted_proj = self.proj * weights.unsqueeze(-1)
        
        # Project input tensor
        return F.linear(x * scale, weighted_proj, self.proj_bias), weighted_proj

    def compute_kl(self, x_proj: torch.Tensor) -> torch.Tensor:
        """
        Compute KL divergence between projected distribution and reference.
        
        Args:
            x_proj: Projected tensor
            
        Returns:
            KL divergence scalar
        """
        # Cache reference params for efficiency in evaluation mode
        if not self.training:
            if self._cached_ref_mu is None or self._last_batch_size != x_proj.size(0):
                self._cached_ref_mu = self.ref_mu.view(1, 1, -1)
                self._cached_ref_sigma = self.ref_sigma.view(1, 1, -1)
                self._last_batch_size = x_proj.size(0)
            ref_mu = self._cached_ref_mu
            ref_sigma = self._cached_ref_sigma
        else:
            ref_mu = self.ref_mu.view(1, 1, -1)
            ref_sigma = self.ref_sigma.view(1, 1, -1)
        
        # Compute statistics with improved numerical stability
        mu = x_proj.mean(dim=(0, 1), keepdim=True)
        sigma = torch.clamp(x_proj.std(dim=(0, 1), unbiased=False, keepdim=True), min=self.eps)
        
        # Compute KL divergence with numerical safeguards
        kl = torch.log(sigma / ref_sigma + self.eps) + \
             (ref_sigma**2 + (ref_mu - mu)**2) / (2 * sigma**2) - 0.5
             
        return kl.mean()

    def compute_correction(self, 
                           x_proj: torch.Tensor, 
                           weighted_proj: torch.Tensor, 
                           scale: float) -> torch.Tensor:
        """
        Compute correction based on divergence from reference distribution.
        
        Args:
            x_proj: Projected tensor
            weighted_proj: Weighted projection matrix
            scale: Scale factor for this projection
            
        Returns:
            Correction tensor for input space
        """
        # Compute mean in projected space
        mu = x_proj.mean(dim=(0, 1), keepdim=True)
        
        # Compute deviation from reference
        delta = mu - self.ref_mu.view(1, 1, -1)
        
        # Adaptive learning rate based on deviation magnitude
        # Clamp to avoid extreme values
        delta_mean = delta.abs().mean()
        adaptive_alpha = self.alpha * torch.clamp(delta_mean, 0.05, 10.0)
        
        # Compute correction by projecting back to input space
        correction = adaptive_alpha * torch.matmul(delta, weighted_proj.T)
        
        # Apply scale factor and reshape
        correction = correction.squeeze(0).squeeze(0) * scale
        
        # Apply gradient clipping if specified
        if self.training and self.gradient_clip_value is not None:
            correction = torch.clamp(correction, 
                                    -self.gradient_clip_value, 
                                    self.gradient_clip_value)
            
        return correction

    def compress(self, corr: torch.Tensor) -> torch.Tensor:
        """
        Compress correction values for memory efficiency.
        
        Args:
            corr: Correction tensor
            
        Returns:
            Compressed correction tensor
        """
        return torch.round(corr * self.compression_factor) / self.compression_factor

    def normalize(self, x: torch.Tensor) -> torch.Tensor:
        """
        Normalize input tensor.
        
        Args:
            x: Input tensor
            
        Returns:
            Normalized tensor
        """
        # Compute statistics along feature dimension
        mean = x.mean(dim=-1, keepdim=True)
        std = x.std(dim=-1, keepdim=True)
        
        # Update variance EMA in training mode
        if self.training:
            var = std.mean()
            self.variance_ema = self.ema_decay * self.variance_ema + (1 - self.ema_decay) * var
        
        # Normalize with stability constant
        std = torch.clamp(std, min=self.eps * 10)
        return (x - mean) / (std + self.eps)

    def update_reference(self, kl: torch.Tensor) -> None:
        """
        Update reference distribution tracking and training status.
        
        Args:
            kl: Current KL divergence value
        """
        # Skip in evaluation mode
        if not self.training:
            return
            
        # Update KL EMA
        self.kl_ema = self.ema_decay * self.kl_ema + (1 - self.ema_decay) * kl.detach()
        
        # Dynamic adjustment of reference trainability
        if not self.ref_mu.requires_grad and self.kl_ema > 2 * self.kl_threshold:
            self.ref_mu.requires_grad = True
            self.ref_sigma.requires_grad = True
        elif self.ref_mu.requires_grad and self.kl_ema <= self.kl_threshold:
            self.ref_mu.requires_grad = False
            self.ref_sigma.requires_grad = False

    def forward(self, x: torch.Tensor, return_dict: bool = False) -> Union[torch.Tensor, Dict[str, Any]]:
        """
        Forward pass through the adaptive layer.
        
        Args:
            x: Input tensor of shape [batch_size, seq_len, hidden_dim]
            return_dict: If True, return detailed diagnostics dictionary
            
        Returns:
            Output tensor or diagnostics dictionary
        """
        # Initialize corrected input and tracking lists
        x_corr = x
        corrections = []
        kl_list = []

        # Adjust threshold based on current variance
        threshold = self.kl_threshold * (1.0 + self.variance_ema)

        # Process each scale
        for idx, scale in enumerate(self.scales):
            # Stop if we've reached max corrections
            if len(corrections) >= self.max_corrections:
                break

            # Project to reference space
            x_proj, weighted_proj = self._project(x_corr, idx, scale)
            
            # Compute KL divergence
            kl = self.compute_kl(x_proj)
            kl_list.append(kl.item())
            
            # Update reference tracking
            self.update_reference(kl)

            # Skip correction in evaluation or monitor-only mode
            if not self.training or self.monitor_only:
                continue

            # Apply correction if KL exceeds threshold
            if kl > threshold:
                # Compute correction
                correction = self.compute_correction(x_proj, weighted_proj, scale)
                
                # Apply correction tentatively
                x_post = x_corr + correction
                
                # Check if correction helps
                x_proj_post, _ = self._project(x_post, idx, scale)
                kl_post = self.compute_kl(x_proj_post)
                
                # Only keep correction if it reduces KL
                if kl_post < kl:
                    x_corr = x_post
                    corrections.append(self.compress(correction.detach()))

        # Update history buffers
        if corrections:
            self.correction_buffer = torch.sum(torch.stack(corrections), dim=0)
            self.compressed_history = torch.stack(corrections)
        else:
            # Initialize with zero tensor of proper shape if no corrections
            if self.correction_buffer.shape != x.shape[-1:]:
                self.correction_buffer = torch.zeros(x.shape[-1], device=x.device)
            self.compressed_history = None

        # Apply normalization and learnable parameters
        x_norm = self.normalize(x_corr)
        out = x_norm * self.gamma + self.beta

        # Return detailed info if requested
        if return_dict:
            return {
                "output": out,
                "kl_values": kl_list,
                "corrections": corrections,
                "kl_ema": self.kl_ema.item(),
                "variance_ema": self.variance_ema.item(),
                "ref_mu": self.ref_mu.detach(),
                "ref_sigma": self.ref_sigma.detach(),
                "correction_count": len(corrections)
            }
        return out

    def rollback(self, step: int = -1) -> torch.Tensor:
        """
        Generate tensor to undo corrections.
        
        Args:
            step: Which correction step to undo (-1 for all)
            
        Returns:
            Tensor that can be added to outputs to undo corrections
        """
        if self.compressed_history is None:
            # Return zero tensor with proper shape
            return torch.zeros(self.hidden_dim, device=self.gamma.device)
        
        if step == -1:
            return -self.correction_buffer
        else:
            # Validate step index
            if step >= 0 and step < len(self.compressed_history):
                return -self.compressed_history[step]
            else:
                warnings.warn(f"Invalid rollback step {step}, must be -1 or in range [0, {len(self.compressed_history)-1}]")
                return torch.zeros(self.hidden_dim, device=self.gamma.device)

    def get_sparsity_loss(self) -> torch.Tensor:
        """
        Calculate sparsity regularization loss.
        
        Returns:
            Sparsity loss tensor
        """
        return self.proj_sparsity * torch.abs(self.proj_weights).mean()

    def freeze_reference(self) -> None:
        """
        Freeze reference distribution parameters.
        """
        self.ref_mu.requires_grad = False
        self.ref_sigma.requires_grad = False

    def reset_stats(self) -> None:
        """
        Reset all statistics tracking.
        """
        self.kl_ema.fill_(0.0)
        self.variance_ema.fill_(1.0)
        self.info_loss_ema.fill_(0.0)
        self.correction_buffer.fill_(0.0)
        self.compressed_history = None
        self._cached_ref_mu = None
        self._cached_ref_sigma = None

    def extra_repr(self) -> str:
        """
        Enhanced string representation with additional parameters.
        
        Returns:
            String representation of layer configuration
        """
        return (f"hidden_dim={self.hidden_dim}, ref_dim={self.ref_dim}, alpha={self.alpha}, "
                f"eps={self.eps}, kl_threshold={self.kl_threshold}, "
                f"trainable_reference={self.ref_mu.requires_grad}, "
                f"monitor_only={self.monitor_only}, "
                f"gradient_clip={self.gradient_clip_value}")


# Example usage:
"""
# Create layer
adaptive_layer = AdaptiveBiasReflectiveLayerV7(
    hidden_dim=768,
    ref_dim=32,
    alpha=0.01,
    kl_threshold=0.15
)

# Forward pass
x = torch.randn(16, 24, 768)  # [batch, seq_len, hidden_dim]
output = adaptive_layer(x)

# Or with diagnostics
diagnostics = adaptive_layer(x, return_dict=True)
output = diagnostics["output"]
kl_values = diagnostics["kl_values"]
"""

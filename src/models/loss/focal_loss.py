import torch


def focal_loss(inputs: torch.Tensor, targets: torch.Tensor, alpha: float = 0.8, gamma: float = 2) -> torch.Tensor:
    """
    Compute the focal loss between `inputs` and the ground truth `targets`.
    Focal loss = -alpha_t * (1-pt)^gamma * log(pt)
    where pt is the probability of being classified to the true class.

    Args:
        inputs: A float tensor of arbitrary shape.
        targets: A float tensor with the same shape as inputs.
        alpha: A float specifying the weighting factor.
        gamma: A float specifying the focusing parameter.

    Returns:
        loss: A float32 scalar representing normalized total loss.
    """
    # first compute binary cross-entropy
    x = inputs.view(-1)
    y = targets.view(-1)
    BCE = (y-1)*torch.max(torch.log(1-x), torch.tensor(-100.).type_as(x)) - \
        y*torch.max(torch.log(x), torch.tensor(-100.).type_as(x))
    BCE = BCE.mean()*inputs.numel()/len(inputs)  # batch mean

    BCE_EXP = torch.exp(-BCE)
    focal_loss = alpha * (1-BCE_EXP)**gamma * BCE

    return focal_loss

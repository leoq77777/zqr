"""Common research tool gateway (docs/contracts-v2.md section 9), shared by the CLI and agent adapters."""

from quant_research.gateway.service import GatewayError, ToolGateway

__all__ = ["GatewayError", "ToolGateway"]

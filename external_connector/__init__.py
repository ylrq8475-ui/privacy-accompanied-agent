"""Phase 1A external-connector boundary exports."""

from .contracts import (
    ApprovedExternalRequest,
    ConnectorAuditRecord,
    ConnectorRejectedError,
    ExternalConnectorBoundary,
    ExternalConnectorRequest,
    ExternalConnectorResponse,
    validate_response_size,
)
from .mock import (
    ListConnectorAuditSink,
    MockConnectorTransportError,
    MockExternalConnector,
    MockWeatherService,
)
from .weather import (
    ExternalConnectorTransportError,
    RealExternalConnector,
    UrllibWeatherTransport,
)

__all__ = [
    "ApprovedExternalRequest",
    "ConnectorAuditRecord",
    "ConnectorRejectedError",
    "ExternalConnectorBoundary",
    "ExternalConnectorRequest",
    "ExternalConnectorResponse",
    "ListConnectorAuditSink",
    "MockConnectorTransportError",
    "MockExternalConnector",
    "MockWeatherService",
    "validate_response_size",
    "ExternalConnectorTransportError",
    "RealExternalConnector",
    "UrllibWeatherTransport",
]

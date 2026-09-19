"""Report engine package: period resolution, aggregation, generation, PDF."""
from .periods import resolve, parse_key, ResolvedPeriod  # noqa: F401
from .aggregate import build_payload  # noqa: F401
from .service import generate_report, render_report_html  # noqa: F401

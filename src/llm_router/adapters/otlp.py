"""Optional OTLP exporter. Imported only when `run_job.trace_exporter` is `otlp` or `both`."""


def _load_exporter():
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    return OTLPSpanExporter, Resource, TracerProvider, SimpleSpanProcessor


def export_span(span, endpoint=None):
    """Send one span to an OTLP HTTP endpoint. Requires the `otlp` extra."""
    try:
        OTLPSpanExporter, Resource, TracerProvider, SimpleSpanProcessor = _load_exporter()
    except ImportError as error:
        raise RuntimeError('OTLP export needs the optional extra: pip install "dynamic-llm-router[otlp]"') from error
    target = endpoint or 'http://localhost:4318/v1/traces'
    provider = TracerProvider(resource=Resource.create({'service.name': 'dynamic-llm-router'}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=target)))
    try:
        tracer = provider.get_tracer('llm_router.adapters.otlp')
        with tracer.start_as_current_span(str((span or {}).get('name') or 'span')) as current:
            for key in ('worker', 'model', 'tokens', 'cost_estimate', 'exit_status', 'task_class', 'trace_id'):
                value = (span or {}).get(key)
                if value is None:
                    continue
                if type(value) not in (str, int, float, bool):
                    value = str(value)
                current.set_attribute(key, value)
        provider.force_flush()
    finally:
        provider.shutdown()
    return {'ok': True, 'endpoint': target}

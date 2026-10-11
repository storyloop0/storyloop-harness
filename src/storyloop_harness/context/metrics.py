"""Aggregate diagnostics for existing telemetry; never emit source text or IDs."""
def record_projection_metrics(span, report):
    for field in ('system_tokens', 'material_tokens', 'schema_tokens', 'transport_tokens',
                  'output_tokens', 'safety_margin_tokens', 'total_tokens', 'remaining_tokens'):
        span.metric('story.context.estimated_' + field, float(getattr(report, field)))
    span.metric('story.context.selected_items', float(sum(d.selected for d in report.decisions)))
    span.metric('story.context.omitted_items', float(sum(not d.selected for d in report.decisions)))
    span.metric('story.context.truncated_items', float(sum(d.truncated for d in report.decisions)))

"""Scope labels for automatic policy evidence; semantic review stays manual."""

DUPLICATE_SCOPE = ('NFKC+casefold Unicode alphanumerics; complete dialogue of at least '
                   '8 normalized characters inside one narration beat')


def acceptance(*, passed: bool, content_verified: bool) -> dict:
    return {
        'passed_scope': 'runtime_contract_only',
        'runtime_contract': 'passed' if passed else 'failed',
        'structural_content': 'passed' if content_verified else 'unavailable',
        'bounded_duplicate_check': 'passed' if content_verified else 'unavailable',
        'duplicate_scope': DUPLICATE_SCOPE,
        'semantic_quality': 'unreviewed',
    }

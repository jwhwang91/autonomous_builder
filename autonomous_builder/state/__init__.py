"""State layer: durable run state, handoffs, logs and the live dashboard.

All builder state lives under the configured data directory (default
``runtime_data/``) inside the *builder* repository — never inside the target
repository, so orchestration never pollutes target history.
"""

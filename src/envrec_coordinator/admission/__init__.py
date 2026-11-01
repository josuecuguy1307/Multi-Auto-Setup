"""The admission exam (docs/RECOMMENDATION_SPEC.md section 8): suites per task type graded by code only, a pass threshold, an early stop, the result stored as a tested fact for the exact
combination, and the owner decision that has to come first. The exam is a fixed, ordered instrument: the model under test answers prompts, code grades them, and nothing here chooses
a model's action or a route (the router reads the facts; it lives in `envrec_coordinator/router`)."""

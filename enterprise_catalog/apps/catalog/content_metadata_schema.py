"""
JSON Schema definitions for content metadata received from the discovery service.

These schemas define the *minimum* required fields for each content type. They are
intentionally permissive (``additionalProperties`` is not restricted) because the
discovery service frequently adds new fields and we do not want ingestion to break
when it does.

The purpose of schema validation is observability and early warning: violations are
logged but never raise exceptions that would block catalog-content inclusion.

Schema design principles:
- Only require fields that *this service* reads to function (content_key, content_type, etc.)
- ``additionalProperties: true`` — accept unknown fields silently
- Use ``type: ["string", "null"]`` for optional-but-typed fields (nullable in practice)
- Never require fields that are only used for Algolia display (title, image_url, etc.)
"""

# Fields required for ALL content types to be processable by this service.
_COMMON_REQUIRED_PROPERTIES = {
    'aggregation_key': {
        'type': 'string',
        'description': (
            'Identifies the content type and key, e.g. "course:edX+DemoX". '
            'Used by get_content_type() and get_parent_content_key().'
        ),
    },
    'uuid': {
        'type': 'string',
        'description': 'UUID of the content. Used as the content_key for programs.',
    },
}

# Schema for a single course run nested inside a course object.
_COURSE_RUN_NESTED_SCHEMA = {
    'type': 'object',
    'required': ['key', 'uuid', 'status'],
    'properties': {
        'key': {'type': 'string'},
        'uuid': {'type': 'string'},
        'status': {'type': 'string'},
        'availability': {'type': ['string', 'null']},
        'is_enrollable': {'type': ['boolean', 'null']},
        'start': {'type': ['string', 'null']},
        'end': {'type': ['string', 'null']},
    },
    'additionalProperties': True,
}

COURSE_METADATA_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'CourseContentMetadata',
    'description': 'Minimum schema for course metadata from discovery /search/all',
    'type': 'object',
    # 'key' is required: it is the content_key for courses (used by get_content_key()).
    # 'content_type' is optional because get_content_type() derives the type from aggregation_key.
    'required': list(_COMMON_REQUIRED_PROPERTIES.keys()) + ['key'],
    'properties': {
        **_COMMON_REQUIRED_PROPERTIES,
        'key': {
            'type': 'string',
            'description': 'Course key, e.g. "edX+DemoX".',
        },
        'content_type': {
            'type': 'string',
        },
        'course_runs': {
            'type': 'array',
            'items': _COURSE_RUN_NESTED_SCHEMA,
        },
        'advertised_course_run_uuid': {
            'type': ['string', 'null'],
        },
    },
    'additionalProperties': True,
}

COURSE_RUN_METADATA_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'CourseRunContentMetadata',
    'description': 'Minimum schema for course run metadata from discovery /search/all',
    'type': 'object',
    'required': list(_COMMON_REQUIRED_PROPERTIES.keys()) + ['key'],
    'properties': {
        **_COMMON_REQUIRED_PROPERTIES,
        'key': {
            'type': 'string',
            'description': 'Course run key, e.g. "course-v1:edX+DemoX+1T2024".',
        },
        'content_type': {
            'type': 'string',
        },
        'status': {
            'type': ['string', 'null'],
        },
    },
    'additionalProperties': True,
}

PROGRAM_METADATA_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'ProgramContentMetadata',
    'description': 'Minimum schema for program metadata from discovery /search/all',
    'type': 'object',
    # Programs use uuid as content_key (no 'key' field); aggregation_key and uuid are required.
    'required': list(_COMMON_REQUIRED_PROPERTIES.keys()),
    'properties': {
        **_COMMON_REQUIRED_PROPERTIES,
        'content_type': {
            'type': 'string',
        },
        'type': {
            'type': ['string', 'null'],
            'description': 'Program type, e.g. "MicroMasters".',
        },
        'status': {
            'type': ['string', 'null'],
        },
    },
    'additionalProperties': True,
}

LEARNER_PATHWAY_METADATA_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'LearnerPathwayContentMetadata',
    'description': 'Minimum schema for learner pathway metadata from discovery /search/all',
    'type': 'object',
    'required': list(_COMMON_REQUIRED_PROPERTIES.keys()),
    'properties': {
        **_COMMON_REQUIRED_PROPERTIES,
        'content_type': {
            'type': 'string',
        },
        'status': {
            'type': ['string', 'null'],
        },
        'visible_via_association': {
            'type': ['boolean', 'null'],
        },
    },
    'additionalProperties': True,
}

# Map content_type strings to their corresponding schema.
CONTENT_TYPE_SCHEMA_MAP = {
    'course': COURSE_METADATA_SCHEMA,
    'courserun': COURSE_RUN_METADATA_SCHEMA,
    'program': PROGRAM_METADATA_SCHEMA,
    'learnerpathway': LEARNER_PATHWAY_METADATA_SCHEMA,
}

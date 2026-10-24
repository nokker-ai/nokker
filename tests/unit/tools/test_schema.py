from nokker.tools.schema import compact_schema


def test_compaction_drops_noise_and_keeps_meaning():
    raw = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "additionalProperties": False,
           "title": "Args", "required": ["url"],
           "properties": {"url": {"type": "string", "title": "Url"},
                          "title": {"type": "string", "description": "titre de la page"},
                          "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None,
                                    "description": "max"},
                          "mode": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}
    out = compact_schema(raw)
    assert out == {"type": "object", "required": ["url"],
                   "properties": {"url": {"type": "string"},
                                  "title": {"type": "string", "description": "titre de la page"},
                                  "limit": {"description": "max", "type": "integer"},
                                  "mode": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}


def test_additional_properties_schema_is_kept():
    raw = {"type": "object", "additionalProperties": {"type": "string"}}
    assert compact_schema(raw) == raw

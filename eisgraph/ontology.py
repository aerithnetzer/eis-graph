"""The fixed EIS ontology Claude extracts against, plus the JSON schemas for both batch phases."""

NS = "https://example.org/eis/"  # vocabulary namespace (eis:)
DATA = "https://example.org/eis/id/"  # instance namespace

# Entity classes. GLOBAL types get one URI across all documents (merged by canonical label);
# LOCAL types are scoped to the document that mentions them ("Alternative 2" in doc A != doc B).
CLASSES = {
    "Project": ("local", "The proposed federal action / project the EIS evaluates."),
    "Alternative": ("local", "A named alternative, including No Action and the Preferred Alternative."),
    "Impact": ("local", "A specific environmental effect of an alternative or project."),
    "MitigationMeasure": ("local", "A measure to avoid, minimize, or compensate for an impact."),
    "Facility": ("local", "Built infrastructure: dam, highway segment, pipeline, mine, plant, transmission line."),
    "Permit": ("local", "A permit, license, authorization, or consultation outcome (e.g. Section 404 permit, Biological Opinion)."),
    "Agency": ("global", "A government agency or sub-unit (federal, state, tribal, local)."),
    "Organization": ("global", "Non-government organization, company, consultant, tribe-as-organization, utility."),
    "Person": ("global", "A named individual (preparer, responsible official, commenter)."),
    "Location": ("global", "A named place: state, county, city, national forest, park, watershed, region."),
    "Waterbody": ("global", "A named river, lake, stream, aquifer, bay, wetland complex."),
    "Species": ("global", "A plant or animal species or population (use the common name; give scientific name as an attribute)."),
    "Habitat": ("global", "A habitat or ecosystem type (e.g. riparian forest, sagebrush steppe, critical habitat)."),
    "ResourceTopic": ("global", "An environmental resource category analyzed (air quality, cultural resources, environmental justice, noise...)."),
    "Pollutant": ("global", "A substance or emission (PM2.5, NOx, mercury, greenhouse gases, sediment)."),
    "Statute": ("global", "A law, regulation, executive order, or treaty (NEPA, ESA Section 7, Clean Water Act Section 404, EO 12898)."),
    "Document": ("global", "Another referenced document: prior EIS, EA, ROD, plan, study, Biological Opinion."),
}

# Object properties (entity -> entity). Domain/range are guidance for the model, not enforced.
RELATIONS = {
    "leadAgency": "Project -> Agency leading the NEPA review.",
    "cooperatingAgency": "Project -> Agency (or Organization/tribe) cooperating on the EIS.",
    "proponent": "Project -> Organization/Agency proposing or applying for the action.",
    "preparedBy": "THIS_DOCUMENT -> Organization/Agency/Person that prepared it.",
    "evaluates": "THIS_DOCUMENT -> Project it analyzes.",
    "hasAlternative": "Project -> Alternative.",
    "includesFacility": "Project/Alternative -> Facility.",
    "locatedIn": "Project/Facility/Location/Waterbody -> Location.",
    "partOf": "Location/Waterbody/Habitat -> larger Location/Waterbody.",
    "affects": "Project/Alternative/Facility -> any resource (Waterbody, Species, Habitat, ResourceTopic, Location).",
    "hasImpact": "Project/Alternative -> Impact.",
    "impactOn": "Impact -> the Species/Waterbody/Habitat/ResourceTopic/Location it falls on.",
    "mitigatedBy": "Impact/Project -> MitigationMeasure.",
    "emits": "Project/Facility/Alternative -> Pollutant.",
    "occursIn": "Species -> Location/Waterbody/Habitat where it occurs.",
    "listedUnder": "Species -> Statute (e.g. ESA) it is protected under.",
    "subjectTo": "Project -> Statute it must comply with.",
    "requiresPermit": "Project/Alternative -> Permit.",
    "issuedBy": "Permit -> Agency that issues it.",
    "authorizedUnder": "Permit -> Statute.",
    "consultedWith": "Agency/Project -> Agency/Organization consulted (Section 7, Section 106, tribal consultation).",
    "managedBy": "Location/Facility/Waterbody -> Agency/Organization managing it.",
    "references": "THIS_DOCUMENT -> Document it cites or relies on.",
    "supplements": "THIS_DOCUMENT -> earlier Document it supplements or revises.",
    "tiersFrom": "THIS_DOCUMENT -> programmatic Document it tiers from.",
    "relatedTo": "Fallback when no predicate above fits; put the specific relation in `note`.",
}

# Datatype properties (entity -> literal).
ATTRIBUTES = {
    "documentStage": "THIS_DOCUMENT: Draft, Final, Supplemental Draft, Supplemental Final, Record of Decision, Programmatic.",
    "date": "Publication / decision date (ISO yyyy-mm-dd or yyyy if that's all that's given).",
    "identifier": "EIS number, CEQ/EPA number, docket, report number, permit number.",
    "isPreferred": "Alternative: 'true' if identified as preferred / proposed action.",
    "isNoAction": "Alternative: 'true' if it is the No Action alternative.",
    "significance": "Impact: none | negligible | minor | moderate | major | significant | unknown.",
    "direction": "Impact: adverse | beneficial | mixed.",
    "duration": "Impact: short-term | long-term | permanent | temporary.",
    "listingStatus": "Species: endangered | threatened | candidate | species of concern | delisted.",
    "scientificName": "Species: binomial name.",
    "quantity": "Any measured magnitude with units (acres disturbed, miles, tons/yr, cost in $).",
    "description": "A one-sentence description of the entity.",
}

RESERVED_DOC_KEY = "THIS_DOCUMENT"

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entities", "relations", "attributes"],
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "label", "type", "aliases"],
                "properties": {
                    "key": {"type": "string", "description": "Short local id, e.g. 'usace' or 'alt2'."},
                    "label": {"type": "string", "description": "Full canonical name as written in the document."},
                    "type": {"type": "string", "enum": list(CLASSES)},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "predicate", "object", "evidence", "note"],
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string", "enum": list(RELATIONS)},
                    "object": {"type": "string"},
                    "evidence": {"type": "string", "description": "Verbatim supporting quote, <= 200 chars."},
                    "note": {"type": "string", "description": "Required specific relation if predicate is relatedTo, else ''."},
                },
            },
        },
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["subject", "property", "value"],
                "properties": {
                    "subject": {"type": "string"},
                    "property": {"type": "string", "enum": list(ATTRIBUTES)},
                    "value": {"type": "string"},
                },
            },
        },
    },
}

RESOLUTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["clusters"],
    "properties": {
        "clusters": {
            "type": "array",
            "description": "Only groups of 2+ input ids that denote the same real-world entity.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["canonical_label", "member_ids"],
                "properties": {
                    "canonical_label": {"type": "string"},
                    "member_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
        }
    },
}

DOC_LINK_PREDICATES = {
    "supplements": "A supplements or revises B (same project, later stage).",
    "sameProject": "A and B are different stages/versions of the same project's EIS.",
    "tiersFrom": "A tiers from programmatic EIS B.",
    "references": "A explicitly cites B.",
    "sharesGeography": "A and B analyze overlapping places or watersheds.",
    "similarActionType": "A and B evaluate the same kind of action (e.g. both are highway widenings, both are forest plan revisions).",
    "cumulativeContext": "B's project is discussed (or would plausibly be) in A's cumulative effects analysis.",
}

DOC_LINK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["links", "reference_matches"],
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "predicate", "target", "rationale"],
                "properties": {
                    "source": {"type": "string", "description": "Document barcode."},
                    "predicate": {"type": "string", "enum": list(DOC_LINK_PREDICATES)},
                    "target": {"type": "string", "description": "Document barcode."},
                    "rationale": {"type": "string"},
                },
            },
        },
        "reference_matches": {
            "type": "array",
            "description": "Referenced Document entities (by ref id) that are actually one of the sampled documents.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ref_id", "barcode"],
                "properties": {"ref_id": {"type": "integer"}, "barcode": {"type": "string"}},
            },
        },
    },
}


def _bullets(d: dict) -> str:
    return "\n".join(f"- {k}: {v[1] if isinstance(v, tuple) else v}" for k, v in d.items())


EXTRACTION_SYSTEM = f"""You build a knowledge graph from U.S. Environmental Impact Statements (EISs) prepared under NEPA.
You will see the opening pages of one EIS (for context) and then one excerpt of it. The text is OCR output and may
contain recognition errors, broken hyphenation, running headers, and table fragments; read through them.

Extract the entities, relations, and attributes stated or clearly implied in THE EXCERPT ONLY, using this ontology.

Entity types:
{_bullets(CLASSES)}

Relations (subject -> object):
{_bullets(RELATIONS)}

Attributes (subject -> literal):
{_bullets(ATTRIBUTES)}

Rules:
- The document being read is the reserved key "{RESERVED_DOC_KEY}"; never declare it in `entities`, just use it as a subject.
- Every key used in a relation or attribute (except {RESERVED_DOC_KEY}) must be declared in `entities`.
- Use full official names for labels ("U.S. Army Corps of Engineers", not "the Corps"); put short forms and acronyms in `aliases`.
- Prefer substantive, specific facts: who leads and cooperates, what is proposed and where, which alternatives exist,
  which resources/species/waterbodies are affected and how significantly, mitigation, permits, and governing statutes.
  Skip boilerplate (tables of contents, generic definitions, distribution lists) and generic nouns ("the site", "wildlife").
- `evidence` must be a short verbatim quote from the excerpt.
- If the excerpt contains nothing substantive (e.g. an index or blank pages), return empty arrays.
"""

RESOLUTION_SYSTEM = """You perform entity resolution for a knowledge graph built from U.S. Environmental Impact Statements.
You get a numbered list of entity names of one type, gathered from many documents (OCR noise included).
Group ids that refer to the same real-world entity: acronyms vs full names, OCR misspellings, name variants,
sub-unit vs parent ONLY if the names are truly the same body (do NOT merge "USDA Forest Service" with
"Tonto National Forest"; do NOT merge different Sections of a statute). Pick the most complete official name as
canonical_label. Return only clusters with 2 or more members; singletons are implied."""

DOC_LINK_SYSTEM = f"""You relate U.S. Environmental Impact Statements to each other for a knowledge graph.
You get the profile of one source EIS (barcode, stage, agencies, projects, places, referenced documents) and a few
candidate EISs that a matcher found sharing specific entities with it; each candidate lists what it shares.
For each candidate, decide which of these links hold, in either direction (source -> candidate or candidate -> source):
{_bullets(DOC_LINK_PREDICATES)}
Only assert links the profiles support; give a one-sentence rationale. Shared names are leads, not proof: OCR and
common place names cause false matches. Most candidates warrant at most one or two links, and some none. Do not emit
links for weak or generic similarity (e.g. both are "federal projects" or both mention the same state). Also report
any referenced document (by ref id, from any profile shown) that is in fact the source or one of the candidates."""

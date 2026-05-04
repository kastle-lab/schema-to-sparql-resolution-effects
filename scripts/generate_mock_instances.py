#!/usr/bin/env python3
"""Generate mock instance TTL files for every schema ontology.

The generated data is intentionally synthetic. Its purpose is coverage:
every declared class receives multiple instances, and every declared property
is asserted multiple times with values shaped from its domain and range.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from rdflib import BNode, Graph, Literal, Namespace, RDF, RDFS, OWL, URIRef, XSD


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas"
MOCK_FILE_NAME = "mock_instances.ttl"
MOCK_OCCURRENCES = 10

PROPERTY_TYPES = {
    RDF.Property,
    OWL.ObjectProperty,
    OWL.DatatypeProperty,
    OWL.AnnotationProperty,
    OWL.FunctionalProperty,
    OWL.InverseFunctionalProperty,
    OWL.SymmetricProperty,
    OWL.TransitiveProperty,
}

CLASS_TYPES = {OWL.Class, RDFS.Class}

DATATYPE_URIS = {
    RDFS.Literal,
    RDF.langString,
    XSD.anyURI,
    XSD.base64Binary,
    XSD.boolean,
    XSD.byte,
    XSD.date,
    XSD.dateTime,
    XSD.decimal,
    XSD.double,
    XSD.duration,
    XSD.float,
    XSD.gYear,
    XSD.gYearMonth,
    XSD.hexBinary,
    XSD.int,
    XSD.integer,
    XSD.long,
    XSD.negativeInteger,
    XSD.nonNegativeInteger,
    XSD.nonPositiveInteger,
    XSD.positiveInteger,
    XSD.short,
    XSD.string,
    XSD.time,
    XSD.unsignedByte,
    XSD.unsignedInt,
    XSD.unsignedLong,
    XSD.unsignedShort,
}


def is_schema_builtin(uri: URIRef) -> bool:
    text = str(uri)
    return (
        text.startswith(str(RDF))
        or text.startswith(str(RDFS))
        or text.startswith(str(OWL))
        or text.startswith(str(XSD))
    )


def local_name(uri: URIRef) -> str:
    text = str(uri)
    for separator in ("#", "/", ":"):
        if separator in text:
            tail = text.rsplit(separator, 1)[-1]
            if tail:
                return tail
    return text


def slug(uri: URIRef) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", local_name(uri)).strip("_")
    if not value:
        value = "Resource"
    if value[0].isdigit():
        value = f"n_{value}"
    return value


def rdf_list(graph: Graph, node) -> list:
    items = []
    while node and node != RDF.nil:
        first = graph.value(node, RDF.first)
        if first is not None:
            items.append(first)
        node = graph.value(node, RDF.rest)
    return items


def expression_uris(graph: Graph, node) -> list[URIRef]:
    if isinstance(node, URIRef):
        return [node]
    if not isinstance(node, BNode):
        return []

    uris: list[URIRef] = []
    for predicate in (OWL.unionOf, OWL.intersectionOf, OWL.oneOf):
        list_node = graph.value(node, predicate)
        if list_node:
            for item in rdf_list(graph, list_node):
                uris.extend(expression_uris(graph, item))

    for predicate in (OWL.complementOf, OWL.someValuesFrom, OWL.allValuesFrom, OWL.onClass, OWL.onDataRange):
        value = graph.value(node, predicate)
        if value:
            uris.extend(expression_uris(graph, value))

    return unique(uris)


def unique(values: Iterable) -> list:
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def declared_classes(graph: Graph) -> list[URIRef]:
    classes = set()
    for class_type in CLASS_TYPES:
        classes.update(s for s in graph.subjects(RDF.type, class_type) if isinstance(s, URIRef))
    classes.update(
        s for s in graph.subjects(RDFS.subClassOf, None) if isinstance(s, URIRef) and not is_schema_builtin(s)
    )
    return sorted(classes, key=str)


def declared_properties(graph: Graph) -> list[URIRef]:
    properties = set()
    for prop_type in PROPERTY_TYPES:
        properties.update(s for s in graph.subjects(RDF.type, prop_type) if isinstance(s, URIRef))
    properties.update(s for s in graph.subjects(RDFS.domain, None) if isinstance(s, URIRef))
    properties.update(s for s in graph.subjects(RDFS.range, None) if isinstance(s, URIRef))
    return sorted(properties, key=str)


def namespace_text(uri: URIRef) -> str:
    text = str(uri)
    hash_index = text.rfind("#")
    slash_index = text.rfind("/")
    split_index = max(hash_index, slash_index)
    if split_index == -1:
        return f"{text}/"
    return text[: split_index + 1]


def ontology_base(graph: Graph, schema_path: Path) -> str:
    ontology = next((s for s in graph.subjects(RDF.type, OWL.Ontology) if isinstance(s, URIRef)), None)
    if ontology:
        base = str(ontology)
    else:
        default_ns = graph.namespace_manager.store.namespace("")
        base = str(default_ns) if default_ns else f"https://example.org/{schema_path.parent.name}/"
    if not base.endswith(("/", "#")):
        base = f"{base}/"
    return base


def instance_base(graph: Graph, classes: list[URIRef], schema_path: Path) -> str:
    namespaces = [(prefix or "", str(namespace)) for prefix, namespace in graph.namespaces()]
    resource_namespaces = [
        namespace
        for prefix, namespace in namespaces
        if "resource" in prefix.lower() or "/resource" in namespace.lower() or "-resource" in namespace.lower()
    ]
    if resource_namespaces:
        return sorted(resource_namespaces, key=len)[0]

    counts: defaultdict[str, int] = defaultdict(int)
    for class_uri in classes:
        counts[namespace_text(class_uri)] += 1
    if counts:
        return sorted(counts, key=lambda namespace: (-counts[namespace], namespace))[0]

    return ontology_base(graph, schema_path)


def datatype_literal(datatype: URIRef | None, prop: URIRef, index: int, data_base: str) -> Literal:
    label = f"Sample {slug(prop)} value {index + 1}"
    if datatype in {XSD.boolean}:
        return Literal(index % 2 == 0)
    if datatype in {XSD.integer, XSD.int, XSD.long, XSD.short, XSD.byte, XSD.nonNegativeInteger, XSD.positiveInteger, XSD.unsignedInt, XSD.unsignedLong, XSD.unsignedShort, XSD.unsignedByte}:
        return Literal(42 + index, datatype=datatype)
    if datatype in {XSD.decimal, XSD.double, XSD.float}:
        return Literal(f"{42 + index}.5", datatype=datatype)
    if datatype == XSD.date:
        return Literal(f"2026-05-{index + 1:02d}", datatype=XSD.date)
    if datatype == XSD.dateTime:
        return Literal(f"2026-05-{index + 1:02d}T12:00:00Z", datatype=XSD.dateTime)
    if datatype == XSD.time:
        return Literal(f"12:{index:02d}:00", datatype=XSD.time)
    if datatype == XSD.duration:
        return Literal(f"P{index + 1}D", datatype=XSD.duration)
    if datatype in {XSD.gYear, XSD.gYearMonth}:
        return Literal(str(2026 + index) if datatype == XSD.gYear else f"2026-{index + 1:02d}", datatype=datatype)
    if datatype == XSD.anyURI:
        return Literal(f"{data_base}{slug(prop)}Value{index + 1:02d}", datatype=XSD.anyURI)
    if datatype == XSD.hexBinary:
        return Literal(f"{index + 15:02X}", datatype=XSD.hexBinary)
    if datatype == XSD.base64Binary:
        return Literal("TQ==", datatype=XSD.base64Binary)
    if datatype == RDF.langString:
        return Literal(label, lang="en")
    if datatype and datatype != RDFS.Literal:
        return Literal(label, datatype=datatype)
    return Literal(label)


def add_prefixes(source: Graph, target: Graph) -> None:
    for prefix, namespace in source.namespaces():
        target.bind(prefix, namespace)


def build_mock_graph(schema_path: Path) -> tuple[Graph, dict[str, int]]:
    schema_graph = Graph()
    schema_graph.parse(schema_path)

    classes = declared_classes(schema_graph)
    properties = declared_properties(schema_graph)
    data_base = instance_base(schema_graph, classes, schema_path)
    data_ns = Namespace(data_base)

    out = Graph()
    add_prefixes(schema_graph, out)

    instances_for_class: dict[URIRef, list[URIRef]] = {}
    used_names: defaultdict[str, int] = defaultdict(int)

    def resources_for_class(class_uri: URIRef) -> list[URIRef]:
        if class_uri not in instances_for_class:
            name = slug(class_uri)
            used_names[name] += 1
            collision_suffix = "" if used_names[name] == 1 else f"_{used_names[name]}"
            resources = []
            for index in range(MOCK_OCCURRENCES):
                instance = data_ns[f"{name}Instance{index + 1:02d}{collision_suffix}"]
                resources.append(instance)
                out.add((instance, RDF.type, class_uri))
                out.add((instance, RDFS.label, Literal(f"Sample {local_name(class_uri)} instance {index + 1}")))
            instances_for_class[class_uri] = resources
        return instances_for_class[class_uri]

    def resource_for_class(class_uri: URIRef, index: int) -> URIRef:
        return resources_for_class(class_uri)[index % MOCK_OCCURRENCES]

    generic_subjects = [data_ns[f"GenericSubject{index + 1:02d}"] for index in range(MOCK_OCCURRENCES)]
    generic_objects = [data_ns[f"GenericObject{index + 1:02d}"] for index in range(MOCK_OCCURRENCES)]
    for index, generic_subject in enumerate(generic_subjects):
        out.add((generic_subject, RDF.type, OWL.Thing))
        out.add((generic_subject, RDFS.label, Literal(f"Sample generic subject {index + 1}")))
    for index, generic_object in enumerate(generic_objects):
        out.add((generic_object, RDF.type, OWL.Thing))
        out.add((generic_object, RDFS.label, Literal(f"Sample generic object {index + 1}")))

    for class_uri in classes:
        resources_for_class(class_uri)

    property_assertions = 0
    for prop in properties:
        domains = unique(
            uri
            for domain in schema_graph.objects(prop, RDFS.domain)
            for uri in expression_uris(schema_graph, domain)
            if isinstance(uri, URIRef) and not is_schema_builtin(uri)
        )
        ranges = unique(
            uri
            for range_value in schema_graph.objects(prop, RDFS.range)
            for uri in expression_uris(schema_graph, range_value)
            if isinstance(uri, URIRef)
        )

        prop_types = set(schema_graph.objects(prop, RDF.type))
        range_uri = ranges[0] if ranges else None
        has_literal_range = isinstance(range_uri, URIRef) and (range_uri in DATATYPE_URIS or str(range_uri).startswith(str(XSD)))
        is_datatype_property = OWL.DatatypeProperty in prop_types
        is_annotation_property = OWL.AnnotationProperty in prop_types

        class_range = next(
            (
                uri
                for uri in ranges
                if isinstance(uri, URIRef) and uri not in DATATYPE_URIS and not str(uri).startswith(str(XSD))
            ),
            None,
        )

        for index in range(MOCK_OCCURRENCES):
            subject = resource_for_class(domains[0], index) if domains else generic_subjects[index]
            for domain in domains[1:]:
                out.add((subject, RDF.type, domain))

            if is_datatype_property or is_annotation_property or has_literal_range:
                value = datatype_literal(range_uri if has_literal_range else None, prop, index, data_base)
            else:
                value = resource_for_class(class_range, index) if class_range else generic_objects[index]
                for extra_range in ranges:
                    if isinstance(extra_range, URIRef) and extra_range not in DATATYPE_URIS and not str(extra_range).startswith(str(XSD)):
                        out.add((value, RDF.type, extra_range))

            out.add((subject, prop, value))
            property_assertions += 1

            if OWL.SymmetricProperty in prop_types and isinstance(value, URIRef):
                out.add((value, prop, subject))

            for inverse in schema_graph.objects(prop, OWL.inverseOf):
                if isinstance(inverse, URIRef) and isinstance(value, URIRef):
                    out.add((value, inverse, subject))

    # Cover explicit restrictions with one extra assertion where they mention a named property.
    for restriction in schema_graph.subjects(RDF.type, OWL.Restriction):
        prop = schema_graph.value(restriction, OWL.onProperty)
        if not isinstance(prop, URIRef):
            continue
        owners = [s for s in schema_graph.subjects(RDFS.subClassOf, restriction) if isinstance(s, URIRef)]
        if not owners:
            owners = [s for s in schema_graph.subjects(OWL.equivalentClass, restriction) if isinstance(s, URIRef)]
        has_value = schema_graph.value(restriction, OWL.hasValue)
        filler = schema_graph.value(restriction, OWL.someValuesFrom) or schema_graph.value(restriction, OWL.allValuesFrom)
        for index in range(MOCK_OCCURRENCES):
            subject = resource_for_class(owners[0], index) if owners else generic_subjects[index]
            if has_value is not None:
                value = has_value
            else:
                filler_uris = expression_uris(schema_graph, filler) if filler else []
                filler_uri = filler_uris[0] if filler_uris else None
                if filler_uri and (filler_uri in DATATYPE_URIS or str(filler_uri).startswith(str(XSD))):
                    value = datatype_literal(filler_uri, prop, index, data_base)
                elif filler_uri:
                    value = resource_for_class(filler_uri, index)
                else:
                    value = generic_objects[index]
            out.add((subject, prop, value))

    out.add((data_ns["SampleDataset01"], RDFS.comment, Literal(f"Synthetic instance coverage data for {schema_path.parent.name}.")))

    return out, {
        "classes": len(classes),
        "properties": len(properties),
        "property_assertions": property_assertions,
        "triples": len(out),
        "data_base": data_base,
    }


def main() -> None:
    for schema_path in sorted(SCHEMA_ROOT.glob("*/schema.ttl")):
        graph, stats = build_mock_graph(schema_path)
        output_path = schema_path.with_name(MOCK_FILE_NAME)
        ttl = graph.serialize(format="turtle")
        header = (
            "# Auto-generated synthetic instance data.\n"
            f"# Source schema: {schema_path.name}\n"
            f"# Coverage: {stats['classes']} named classes, {stats['properties']} properties, "
            f"{stats['property_assertions']} property assertions, {stats['triples']} triples.\n"
            f"# Occurrences: at least {MOCK_OCCURRENCES} instances per named class and "
            f"at least {MOCK_OCCURRENCES} assertions per declared property.\n\n"
        )
        output_path.write_text(header + ttl, encoding="utf-8")
        print(
            f"{schema_path.parent.name}: wrote {output_path} "
            f"({stats['classes']} named classes, {stats['properties']} properties, {stats['triples']} triples)"
        )


if __name__ == "__main__":
    main()

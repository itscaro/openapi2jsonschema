#!/usr/bin/env python

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import click
import requests
import yaml
from jsonref import JsonRef  # type: ignore


class Response:
    openapi_types = None


from kubernetes.client import ApiClient, Configuration
from kubernetes.config import load_kube_config

from openapi2jsonschema.errors import UnsupportedError
from openapi2jsonschema.log import debug, error, info
from openapi2jsonschema.util import (
    additional_properties,
    allow_null_optional_fields,
    append_no_duplicates,
    change_dict_values,
    replace_int_or_string,
)


@click.command()
@click.option(
    "-o",
    "--output",
    default="schemas",
    metavar="PATH",
    help="Directory to store schema files",
)
@click.option(
    "-p",
    "--prefix",
    default="_definitions.json",
    help="Prefix for JSON references (only for OpenAPI versions before 3.0)",
)
@click.option("--stand-alone", is_flag=True, help="Whether or not to de-reference JSON schemas")
@click.option("--expanded", is_flag=True, help="Expand Kubernetes schemas by API version")
@click.option("--kubernetes", is_flag=True, help="Enable Kubernetes specific processors")
@click.option(
    "--strict",
    is_flag=True,
    help="Prohibits properties not in the schema (additionalProperties: false)",
)
@click.option("--insecure-skip-tls-verify", is_flag=True)
@click.argument("schema", metavar="SCHEMA_URL")
def default(output, prefix, stand_alone, expanded, kubernetes, strict, insecure_skip_tls_verify, schema):
    """
    Converts a valid OpenAPI specification into a set of JSON Schema files
    """

    info("Downloading schema")
    if schema.startswith("kf://"):
        load_kube_config(context=parse_qs(urlparse(schema).query).get("context", [None])[0])
        Configuration._default.verify_ssl = not insecure_skip_tls_verify
        with ApiClient() as api_client:
            data = api_client.call_api(
                "/openapi/v2", "GET", _return_http_data_only=True, response_type=Response, auth_settings=["BearerToken"]
            )
    else:
        if os.path.isfile(schema):
            payload = Path(schema).read_bytes()
        else:
            payload = requests.get(schema).content
        info("Parsing schema")
        # Note that JSON is valid YAML, so we can use the YAML parser whether
        # the schema is stored in JSON or YAML
        data = yaml.safe_load(payload)

    if "swagger" in data:
        version = data["swagger"]
    elif "openapi" in data:
        version = data["openapi"]

    if not os.path.exists(output):
        os.makedirs(output)

    if version < "3":
        info("Generating shared definitions")
        definitions = data["definitions"]
        if kubernetes:
            definitions["io.k8s.apimachinery.pkg.util.intstr.IntOrString"] = {
                "oneOf": [{"type": "string"}, {"type": "integer"}]
            }
            # Although the kubernetes api does not allow `number`  as valid
            # Quantity type - almost all kubenetes tooling
            # recognizes it is valid. For this reason, we extend the API definition to
            # allow `number` values.
            definitions["io.k8s.apimachinery.pkg.api.resource.Quantity"] = {
                "oneOf": [{"type": "string"}, {"type": "number"}]
            }

            # For Kubernetes, populate `apiVersion` and `kind` properties from `x-kubernetes-group-version-kind`
            for type_name in definitions:
                type_def = definitions[type_name]
                if "x-kubernetes-group-version-kind" in type_def:
                    for kube_ext in type_def["x-kubernetes-group-version-kind"]:
                        if expanded and "apiVersion" in type_def.get("properties", {}):
                            api_version = (
                                kube_ext["group"] + "/" + kube_ext["version"]
                                if kube_ext["group"]
                                else kube_ext["version"]
                            )
                            append_no_duplicates(
                                type_def["properties"]["apiVersion"],
                                "enum",
                                api_version,
                            )
                        if "kind" in type_def.get("properties", {}):
                            kind = kube_ext["kind"]
                            append_no_duplicates(type_def["properties"]["kind"], "enum", kind)
        if strict:
            definitions = additional_properties(definitions)
        Path(output, "_definitions.json").write_text(json.dumps({"definitions": definitions}, indent=2))

    types = []

    info("Generating individual schemas")
    if version < "3":
        components = data["definitions"]
    else:
        components = data["components"]["schemas"]

    for title in components:
        kind = title.split(".")[-1].lower()
        if kubernetes:
            group = title.split(".")[-3].lower()
            api_version = title.split(".")[-2].lower()
        specification = components[title]
        specification["$schema"] = "http://json-schema.org/schema#"
        specification.setdefault("type", "object")

        if strict:
            specification["additionalProperties"] = False

        if kubernetes and expanded:
            if group in ["core", "api"]:
                full_name = "%s-%s" % (kind, api_version)
            else:
                full_name = "%s-%s-%s" % (kind, group, api_version)
        else:
            full_name = kind

        types.append(title)

        try:
            debug("Processing %s" % full_name)

            # These APIs are all deprecated
            if kubernetes:
                if title.split(".")[3] == "pkg" and title.split(".")[2] == "kubernetes":
                    raise UnsupportedError("%s not currently supported, due to use of pkg namespace" % title)

            # This list of Kubernetes types carry around jsonschema for Kubernetes and don't
            # currently work with openapi2jsonschema
            if (
                kubernetes
                and stand_alone
                and kind
                in [
                    "jsonschemaprops",
                    "jsonschemapropsorarray",
                    "customresourcevalidation",
                    "customresourcedefinition",
                    "customresourcedefinitionspec",
                    "customresourcedefinitionlist",
                    "customresourcedefinitionspec",
                    "jsonschemapropsorstringarray",
                    "jsonschemapropsorbool",
                    "customresourcedefinitionversion",  # maximum recursion depth exceeded while calling a Python object
                ]
            ):
                raise UnsupportedError("%s not currently supported" % kind)

            updated = change_dict_values(specification, prefix, version)
            specification = updated

            if stand_alone:
                base = "file://%s/%s/" % (os.getcwd(), output)
                specification = JsonRef.replace_refs(specification, base_uri=base)

            if "additionalProperties" in specification:
                if specification["additionalProperties"]:
                    updated = change_dict_values(specification["additionalProperties"], prefix, version)
                    specification["additionalProperties"] = updated

            if strict and "properties" in specification:
                updated = additional_properties(specification["properties"])
                specification["properties"] = updated

            if kubernetes and "properties" in specification:
                updated = replace_int_or_string(specification["properties"])
                updated = allow_null_optional_fields(updated)
                specification["properties"] = updated

            debug("Generating %s.json" % full_name)
            Path(output, f"{full_name}.json").write_text(json.dumps(specification, indent=2))
        except Exception as e:
            error("An error occured processing %s: %s" % (kind, e))

    info("Generating schema for all types")
    contents = {"oneOf": []}
    for title in types:
        if version < "3":
            contents["oneOf"].append({"$ref": "%s#/definitions/%s" % (prefix, title)})
        else:
            contents["oneOf"].append({"$ref": (title.replace("#/components/schemas/", "") + ".json")})
    Path(output, "all.json").write_text(json.dumps(contents, indent=2))


if __name__ == "__main__":
    default()

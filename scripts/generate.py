"""Generate Pydantic DTOs and FastAPI route wrappers from the checked-in contract."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "openapi" / "openapi.yaml"
OUT = ROOT / "generated"


def schema_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def parameter_annotation(parameter: dict) -> tuple[str, str]:
    name = parameter["name"]
    schema = parameter["schema"]
    if "$ref" in schema:
        annotation = f"models.{schema_name(schema['$ref'])}"
    elif schema.get("format") == "uuid":
        annotation = "UUID"
    elif schema.get("type") == "integer":
        annotation = "int"
    else:
        annotation = "str"
    if parameter["in"] == "path":
        return f"{name}: {annotation}", name
    default = schema.get("default")
    if default is None:
        annotation += " | None"
    args = [repr(default)]
    for source, dest in (("minimum", "ge"), ("maximum", "le")):
        if source in schema:
            args.append(f"{dest}={schema[source]}")
    return f"{name}: {annotation} = Query({', '.join(args)})", name


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(
        [
            "datamodel-codegen",
            "--input", str(SPEC),
            "--input-file-type", "openapi",
            "--output", str(OUT / "models.py"),
            "--output-model-type", "pydantic_v2.BaseModel",
            "--field-constraints",
            "--disable-timestamp",
        ],
        check=True,
    )
    contract = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    # datamodel-code-generator 0.34 truncates decimal minimum=0.01 to ge=0.
    # Keep the generated Pydantic constraints faithful to the OpenAPI source.
    model_path = OUT / "models.py"
    model_source = model_path.read_text(encoding="utf-8")
    for name, schema in contract["components"]["schemas"].items():
        constraints = {
            field: prop["minimum"]
            for field, prop in schema.get("properties", {}).items()
            if prop.get("format") == "decimal" and "minimum" in prop
        }
        if not constraints:
            continue
        match = re.search(rf"(?m)^class {re.escape(name)}\(BaseModel\):\n", model_source)
        if match is None:
            raise RuntimeError(f"Generated model missing: {name}")
        end = re.search(r"(?m)^class ", model_source[match.end():])
        stop = match.end() + end.start() if end else len(model_source)
        block = model_source[match.end():stop]
        for field, minimum in constraints.items():
            pattern = rf"(?m)^(    {re.escape(field)}: Decimal = Field\([^\n]*?\bge=)([^,)]+)"
            block, count = re.subn(pattern, lambda found: found.group(1) + f"Decimal('{minimum}')", block)
            if count != 1:
                raise RuntimeError(f"Generated decimal constraint missing: {name}.{field}")
        model_source = model_source[:match.end()] + block + model_source[stop:]
    model_path.write_text(model_source, encoding="utf-8")
    lines = [
        '"""Generated route handlers; edit openapi/openapi.yaml and rerun generation."""',
        "from uuid import UUID",
        "from fastapi import APIRouter, Depends, Query, Request",
        "from app import auth, handlers",
        "from app.auth import Principal",
        "from . import models",
        "router = APIRouter()",
        "",
    ]
    for path, path_item in contract["paths"].items():
        common_params = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            op = operation["operationId"]
            if not re.fullmatch(r"[a-z][a-z0-9_]*", op):
                raise ValueError(f"Invalid operationId: {op}")
            parameters = common_params + operation.get("parameters", [])
            path_params = []
            query_params = []
            arg_names = []
            for parameter in parameters:
                annotation, name = parameter_annotation(parameter)
                (path_params if parameter["in"] == "path" else query_params).append(annotation)
                arg_names.append(name)
            body = operation.get("requestBody")
            body_ref = body["content"]["application/json"]["schema"]["$ref"] if body else None
            public = operation.get("security") == []
            required = path_params + ["request: Request"]
            if body_ref:
                required.append(f"payload: models.{schema_name(body_ref)}")
            optional = query_params
            if not public:
                optional.append("principal: Principal = Depends(auth.current_principal)")
            signature = ", ".join(required + optional)
            handler_args = ["request=request", "principal=None" if public else "principal=principal"]
            if body_ref:
                handler_args.append("payload=payload")
            handler_args += [f"{name}={name}" for name in arg_names]
            lines += [f"def {op}({signature}):", f"    return handlers.{op}({', '.join(handler_args)})"]
            success = next((int(code) for code in operation["responses"] if code.isdigit() and code.startswith("2")), 200)
            response = operation["responses"][str(success)]
            response_ref = response.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
            model = f"models.{schema_name(response_ref)}" if response_ref else "None"
            lines += [
                f"router.add_api_route({path!r}, {op}, methods={[method.upper()]!r}, "
                f"status_code={success}, response_model={model}, name={op!r})",
                "",
            ]
    (OUT / "routes.py").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

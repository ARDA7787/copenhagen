"""Safe YAML decoding with field-path and source-line diagnostics."""

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import ValidationError

from copenhagen.core.capability import Spec

T = TypeVar("T", bound=Spec)


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader: UniqueLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"line {key_node.start_mark.line + 1}: duplicate key {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load[T: Spec](path: Path, model: type[T]) -> T:
    source = path.read_text()
    try:
        value = yaml.load(source, Loader=UniqueLoader)  # noqa: S506 -- subclass of SafeLoader
        return model.model_validate(value)
    except ValidationError as error:
        node = yaml.compose(source)
        lines: dict[tuple, int] = {}

        def visit(current: yaml.Node, prefix: tuple = ()) -> None:
            lines[prefix] = current.start_mark.line + 1
            if isinstance(current, yaml.MappingNode):
                for key, child in current.value:
                    visit(child, (*prefix, key.value))
            elif isinstance(current, yaml.SequenceNode):
                for index, child in enumerate(current.value):
                    visit(child, (*prefix, index))

        if node:
            visit(node)
        messages = []
        for item in error.errors(include_input=False):
            location = item["loc"]
            closest = location
            while closest not in lines and closest:
                closest = closest[:-1]
            messages.append(
                f"{path}:{lines.get(closest, 1)}: {'.'.join(map(str, location))}: {item['msg']}"
            )
        raise ValueError("\n".join(messages)) from None
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: {error}") from None

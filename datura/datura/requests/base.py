import abc
import enum
import json

import pydantic


class ValidationError(Exception):
    def __init__(self, msg):
        self.msg = msg

    @classmethod
    def from_json_decode_error(cls, exc: json.JSONDecodeError):
        return cls(exc.args[0])

    @classmethod
    def from_pydantic_validation_error(cls, exc: pydantic.ValidationError):
        return cls(json.dumps(exc.json()))

    def __repr__(self):
        return f"{type(self).__name__}({self.msg})"


def all_subclasses(cls: type):
    for subcls in cls.__subclasses__():
        yield subcls
        yield from all_subclasses(subcls)


base_class_to_request_type_mapping = {}


class BaseRequest(pydantic.BaseModel, abc.ABC):
    message_type: enum.Enum

    @classmethod
    def type_to_model(cls, type_: enum.Enum) -> type["BaseRequest"]:
        mapping = base_class_to_request_type_mapping.get(cls)
        if not mapping:
            mapping = {}
            for klass in all_subclasses(cls):
                if not (message_type := klass.__fields__.get("message_type")):
                    continue
                if not message_type.default:
                    continue
                mapping[message_type.default] = klass
            base_class_to_request_type_mapping[cls] = mapping

        return mapping[type_]

    @classmethod
    def parse(cls, str_: str):
        try:
            json_ = json.loads(str_)
        except json.JSONDecodeError as exc:
            raise ValidationError.from_json_decode_error(exc)

        try:
            base_model_object = cls.parse_obj(json_)
        except pydantic.ValidationError as exc:
            raise ValidationError.from_pydantic_validation_error(exc)

        target_model = cls.type_to_model(base_model_object.message_type)

        try:
            return target_model.parse_obj(json_)
        except pydantic.ValidationError as exc:
            raise ValidationError.from_pydantic_validation_error(exc)

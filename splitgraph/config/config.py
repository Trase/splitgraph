# engine params
from typing import Optional, Union, Dict, Any, cast

from splitgraph.config import get_singleton
from .argument_config import get_argument_config_value
from .config_file_config import get_config_dict_from_config_file
from .default_config import get_default_config_value
from .environment_config import get_environment_config_value
from .keys import KEYS, ALL_KEYS, ConfigDict
from .system_config import get_system_config_value


def lazy_get_config_value(
    key: str, default_return: Optional[str] = None
) -> Optional[Union[str, Dict[str, Dict[str, str]]]]:
    """
        Get the config value for a key in the following precedence
        Otherwise return default_return
    """

    if key not in ALL_KEYS:
        # For sections which can't be overridden via envvars/arguments,
        # we only use default values
        return get_default_config_value(key, None) or default_return

    return (
        get_argument_config_value(key, None)
        or get_environment_config_value(key, None)
        or get_system_config_value(key, None)
        or get_default_config_value(key, None)
        or default_return
    )


def update_config_dict_from_arguments(config_dict: ConfigDict) -> ConfigDict:
    """
        Given an existing config_dict, update after reading sys.argv
        and overwriting any keys.

        Return updated copy of config_dict.
    """
    argument_config_dict = {
        k: get_argument_config_value(k, None)
        for k in KEYS
        if get_argument_config_value(k) is not None
    }
    new_config_dict = patch_config(config_dict, cast(ConfigDict, argument_config_dict))
    return new_config_dict


def update_config_dict_from_env_vars(config_dict: ConfigDict) -> ConfigDict:
    """
        Given an existing config_dict, update after reading os.environ
        and overwriting any keys.

        Return updated copy of config_dict.
    """

    argument_config_dict = {
        k: get_environment_config_value(k, None)
        for k in KEYS
        if get_environment_config_value(k) is not None
    }
    new_config_dict = patch_config(config_dict, cast(ConfigDict, argument_config_dict))

    return new_config_dict


def update_config_dict_from_file(config_dict: ConfigDict, sg_config_file: str) -> ConfigDict:
    """
        Given an existing config_dict, update after reading sg_config_file
        and overwriting any keys according to the rules in config_file_config

        Return updated copy of config_dict.
    """

    config_file_dict = get_config_dict_from_config_file(sg_config_file)
    new_config_dict = patch_config(config_dict, config_file_dict)

    return new_config_dict


def create_config_dict() -> ConfigDict:
    """
        Create and return a dict of all known config values
    """

    initial_dict = {k: lazy_get_config_value(k) for k in ALL_KEYS}
    config_dict = cast(ConfigDict, {k: v for k, v in initial_dict.items() if v is not None})

    try:
        sg_config_file = get_singleton(config_dict, "SG_CONFIG_FILE")
        config_dict = update_config_dict_from_file(config_dict, sg_config_file)
    except KeyError:
        pass

    config_dict = update_config_dict_from_env_vars(config_dict)
    config_dict = update_config_dict_from_arguments(config_dict)

    return config_dict


def patch_config(config: ConfigDict, patch: ConfigDict) -> ConfigDict:
    """
    Recursively updates a nested configuration dictionary:

    patch_config(
        {"key_1": "value_1",
         "dict_1": {"key_1": "value_1"}},
        {"key_1": "value_2",
         "dict_1": {"key_2": "value_2"}}) == \
        {"key_1": "value_2",
         "dict_1": {"key_1": "value_1", "key_2": "value_2"}}

    :param config: Config dictionary
    :param patch: Dictionary with the path
    :return: New patched dictionary
    """

    def _patch_internal(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in right.items():
            if key in left and isinstance(left[key], dict) and isinstance(value, dict):
                left[key] = _patch_internal(left[key], value)
            else:
                left[key] = value
        return left

    result: ConfigDict = _patch_internal(config.copy(), patch)
    return result

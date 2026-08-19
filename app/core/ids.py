from ulid import ULID


def new_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def new_request_id() -> str:
    return new_id("req")


def new_task_id() -> str:
    return new_id("tsk")


def new_file_id() -> str:
    return new_id("fil")


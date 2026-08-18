"""Core application logic for BuildGuard.

This module currently contains a single status function used to verify
that the application foundation is working correctly. It will be
extended in later stages as new capabilities are added.
"""


def get_buildguard_status() -> str:
    """Return the current status of the BuildGuard application.

    Returns:
        A human-readable status string.
    """
    return "BuildGuard application is running"


if __name__ == "__main__":
    print(get_buildguard_status())

"""Code generation pipeline from IaC provider schemas.

Per DP#10: Type-safe SDK from infrastructure-as-code type generation.
Reads terraform provider schema JSON and generates typed resource handlers.
"""

from .generator import CodeGenerator, generate_handler

__all__ = ["CodeGenerator", "generate_handler"]

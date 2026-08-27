from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """QA 平台所有关系型数据表共享的声明式基类。"""

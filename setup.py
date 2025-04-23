from setuptools import setup, find_packages

setup(
    name="brother_ql_app",
    version="3.0.1",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        "Flask<2.3.0,>=2.0.0",
        "connexion[swagger-ui]==2.14.2",
        "python-dotenv==1.0.0",
        "marshmallow==3.20.1",
        "pydantic==2.4.2",
        "flask-cors==4.0.0",
        "brother-ql==0.9.4",
        "Pillow==10.0.1",
        "structlog==23.1.0",
    ],
    python_requires=">=3.8",
)

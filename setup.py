from setuptools import setup, find_packages

setup(
    name="brother_ql_app",
    version="4.0.0.dev0",
    packages=find_packages(),
    include_package_data=True,
    # Kept in sync with requirements.txt (the authoritative dependency list).
    install_requires=[
        # Core
        "Flask<2.3.0,>=2.0.0",
        "connexion[swagger-ui]==2.14.2",
        "flask-cors==6.0.5",
        # Printer communication
        "brother-ql-inventree==1.3",
        "Pillow==12.3.0",
        "qrcode==7.4.2",
        "pypdfium2==4.30.0",
        # Logging
        "structlog==23.1.0",
    ],
    python_requires=">=3.11",
)

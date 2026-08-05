from setuptools import setup, find_packages

setup(
    name="brother_ql_app",
    version="4.0.1",
    packages=find_packages(),
    include_package_data=True,
    # Kept in sync with requirements.txt (the authoritative dependency list).
    install_requires=[
        # Core
        "connexion[flask,swagger-ui]==3.3.0",
        "Flask==3.1.3",
        "flask-cors==6.0.5",
        # Imported directly by src/app.py, not just transitively via connexion.
        "PyYAML==6.0.3",
        # Printer communication
        "brother-ql-inventree==1.3",
        "Pillow==12.3.0",
        "qrcode==7.4.2",
        "pypdfium2==5.12.1",
        # Server. Connexion 3 is ASGI, so gunicorn serves it through uvicorn's
        # worker class (see docker-entrypoint.sh).
        "gunicorn==23.0.0",
        "uvicorn==0.38.0",
        # Logging
        "structlog==23.1.0",
    ],
    python_requires=">=3.11",
)

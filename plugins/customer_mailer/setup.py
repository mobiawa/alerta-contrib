from setuptools import find_packages, setup

version = '0.1.0'

setup(
    name='alerta-customer-mailer',
    version=version,
    description='Alerta plugin that emails users assigned to alert.customer on first creation',
    url='https://github.com/alerta/alerta-contrib',
    license='MIT',
    packages=find_packages(),
    py_modules=['alerta_customer_mailer'],
    install_requires=[
        'jinja2'
    ],
    include_package_data=True,
    zip_safe=True,
    entry_points={
        'alerta.plugins': [
            'customer_mailer = alerta_customer_mailer:CustomerMailer'
        ]
    }
)

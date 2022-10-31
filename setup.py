#!/usr/bin/env python

from distutils.core import setup

setup(name='Openapi2jsonschema',
      version='1.0',
      description='OpenAPI to JSON Schemas converter',
      author='Yann Hamon',
      author_email='yann@mandragor.org',
      url='http://github.com/yannh/openapi2jsonschema',
      packages=['openapi2jsonschema'],
      install_requires=[
            'click==7.1.2',
            'jsonref==0.2',
            'PyYAML==5.4.1',
      ],
      )

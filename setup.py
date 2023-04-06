#!/usr/bin/env python3
# coding=utf-8


from setuptools import setup

setup(
    name='nexus_uploader',
    version='0.0.1',
    author='fangwentong',
    author_email='fangwentong2012@gmail.com',
    packages=['nexus_uploader'],
    package_dir={'nexus_uploader': ''},
    license='MIT',
    zip_safe=False,
    include_package_data=True,
    entry_points={
        'console_scripts': ['nexus-uploader=nexus_uploader.nexus_uploader:main']
    },
    install_requires=[]
)

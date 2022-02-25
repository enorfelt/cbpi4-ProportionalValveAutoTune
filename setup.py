from setuptools import setup

setup(name='cbpi4-ProportionalValveAutoTune',
      version='0.0.1',
      description='CraftBeerPi Plugin',
      author='',
      author_email='',
      url='',
      include_package_data=True,
      package_data={
        # If any package contains *.txt or *.rst files, include them:
      '': ['*.txt', '*.rst', '*.yaml'],
      'cbpi4-ProportionalValveAutoTune': ['*','*.txt', '*.rst', '*.yaml']},
      packages=['cbpi4-ProportionalValveAutoTune'],
     )
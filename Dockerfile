FROM pytorch/pytorch:1.9.0-cuda10.2-cudnn7-devel
MAINTAINER data-science@shoprunner.com

RUN apt-get update \
    && apt-get install -y tmux \
    && apt-get install -y vim \
    && apt-get install -y libpq-dev \
    && apt-get install -y gcc \
    && apt-get clean

# first remove PyYAML from conda or else pip gives us an error that a distutils library cannot be
# uninstalled
RUN conda remove PyYAML

USER root
WORKDIR /collie/

# copy files to container
COPY setup.py README.md LICENSE requirements-dev.txt ./
COPY collie/_version.py ./collie/

# install libraries
RUN \
  pip3 install -U pip && \
  pip3 install --no-cache-dir -r requirements-dev.txt && \
  pip3 install -e .

# copy the rest of the files over
COPY . .

CMD /bin/bash

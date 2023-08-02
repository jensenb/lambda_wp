FROM python:3.10-slim-bullseye

WORKDIR /code
ADD requirements.txt /tmp
RUN pip install -r /tmp/requirements.txt
ADD config.yaml /code

ADD lambda_modbus.py /code

CMD python3  lambda_modbus.py --log-level "INFO" --config config.yaml

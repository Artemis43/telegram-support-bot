FROM python:3.13-slim
WORKDIR /supportbot
COPY . /supportbot/
RUN pip install -r requirements.txt
EXPOSE 8080
CMD python main.py
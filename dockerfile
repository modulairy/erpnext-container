FROM  frappe/erpnext@sha256:d63c0d83717d53944094df0821c3f65fc2e91cb97a04b487f5d816619887dfe2

USER root
RUN apt update -y && apt upgrade -y
USER frappe

COPY ./resources/. /home/frappe/frappe-bench/.

RUN bench get-app crm
RUN bench get-app helpdesk
RUN bench get-app wiki
RUN bench get-app hrms
RUN bench get-app builder
RUN bench get-app drive
RUN bench get-app books

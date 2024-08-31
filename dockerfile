# v15.33.5
FROM  frappe/erpnext@sha256:d63c0d83717d53944094df0821c3f65fc2e91cb97a04b487f5d816619887dfe2 

#v15.33.4
# FROM  frappe/erpnext@sha256:19a1ab2ebfce992c725432e968d0b6b65b738c3384d27ef1def319b28887f7c5

USER root
RUN apt update -y && apt upgrade -y
USER frappe

COPY ./resources/. /home/frappe/frappe-bench


RUN bench get-app wiki
RUN bench get-app builder
RUN bench get-app drive
RUN bench get-app lending

RUN nvm use --lts
RUN echo $(npm --version) && echo $(node --version) && sleep 4 && exit 1

RUN bench new-site build.test && bench --site build.test localhost

RUN bench get-app insights 

# RUN bench get-app lms


# https://github.com/frappe/insights

# RUN bench get-app books
# RUN bench get-app hrms
# RUN bench get-app --branch v1.19.0 --resolve-deps crm https://github.com/frappe/crm
# RUN bench get-app helpdesk


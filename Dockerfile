FROM nginx:alpine

COPY homepage/ /usr/share/nginx/html/

COPY homepage/nginx.conf /etc/nginx/templates/default.conf.template

ENV PORT=80

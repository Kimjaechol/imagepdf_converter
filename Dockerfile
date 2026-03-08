FROM nginx:alpine

COPY homepage/ /usr/share/nginx/html/

COPY homepage/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

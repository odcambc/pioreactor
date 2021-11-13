<img src="https://user-images.githubusercontent.com/884032/101398418-08e1c700-389c-11eb-8cf2-592c20383a19.png" width="250">
<br />


The Pioreactor is an open-source, affordable, and extensible bioreactor platform. The goal is to enable biologists, educators, DIYers, biohackers, and enthusiasts to be able to reliably control and study microorganisms.

We hope to empower the next generation of builders, similar to the Raspberry Pi's influence on our imagination (in fact, at the core of our hardware _is_ a Raspberry Pi). However, the builders in mind are those who are looking to use biology, or computer science, or both, to achieve their goals. For research, the affordable price point enables fleets of Pioreactors to study large experiment spaces. For educators and students, the Pioreactor is a learning tool to study a wide variety of microbiology, electrical engineering, and computer science principles. For enthusiasts, the control and extensibility of the Pioreactor gives them a platform to build their next project on-top of.



### Where can I get one?

Coming soon! Sign up [here](https://pioreactor.com/) for updates.

### Documentation

All the documentation is [available in our wiki](https://pioreactor.com/pages/documentation).

### Development

#### Images

Images are built in the [Pioreactor/CustoPizer](https://github.com/Pioreactor/CustoPiZer/tree/pioreactor) repo.

#### Local development

```
pip3 install -e .
pip3 install -r requirements/requirements_dev.txt
```


#### Testing

Paho MQTT uses lots of sockets, and running all tests at once can overload the max allowed open files. Try something
like `ulimit -Sn 10000` if you receive `OSError: [Errno 24] Too many open files`

```
py.test pioreactor/tests
```


#### Running jobs locally

```
TESTING=1 pio run <job name>
```

You can also modify to hostname and experiment with

```
TESTING=1 \
HOSTNAME=<whatever> \
EXPERIMENT=<up to you> \
pio run <job name>
```

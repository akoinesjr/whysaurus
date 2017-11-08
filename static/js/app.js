import React from 'react';
import ReactDOM from 'react-dom';
import {PostListWithPoints, PointListWithPoints, PointListWithPoint} from './ys/point_list';

const { Map, List, Seq } = require('immutable');
const prettyI = require("pretty-immutable");


import { gql, graphql, ApolloClient, ApolloProvider } from 'react-apollo';


const dummyPoints = [
  {
    key: 1,
    title: "I am the very model of a modern major general - I understand dynamics and my movement is perpetual",
    numSupporting: 1,
    numCounter: 0,
    upVotes: 48,
    downVotes: 6,
    sources: [],
    supportingPoints: [{
      key: 3,
      title: "I am in fact a general",
      numSupporting: 1,
      numCounter: 1,
      upVotes: 14,
      downVotes: 3,
      sources: [],
      supportingPoints: [],
      counterPoints: [{
        key: 5,
        title: "I do general stuff",
        numSupporting: 0,
        numCounter: 0,
        upVotes: 14,
        downVotes: 3,
        sources: [],
        supportingPoints: [],
        counterPoints: [],
        creatorName: "Trav",
        creatorURL: "http://trav.com",
        numUsersContributed: 12,
        numComments: 2
      }],
      creatorName: "Trav",
      creatorURL: "http://trav.com",
      numUsersContributed: 12,
      numComments: 2
    }],
    counterPoints: [{
      key: 4,
      title: "You aren't a great model",
      numSupporting: 0,
      numCounter: 0,
      upVotes: 40,
      downVotes: 100,
      sources: [],
      supportingPoints: [],
      counterPoints: [],
      creatorName: "Trav",
      creatorURL: "http://trav.com",
      numUsersContributed: 1,
      numComments: 1
    }],
    creatorName: "Trav",
    creatorURL: "http://trav.com",
    numUsersContributed: 3,
    numComments: 4
  },
  {
    key: 2,
    title: "Bacon is the greatest",
    numSupporting: 0,
    numCounter: 0,
    upVotes: 450,
    downVotes: 30,
    sources: [],
    supportingPoints: [],
    counterPoints: [],
    creatorName: "Bakedude",
    creatorURL: "http://bake.com",
    numUsersContributed: 2,
    numComments: 4,
    numSupported: 2,
    imageURL: ""
  }
]

const client = new ApolloClient({});

let templateData = document.getElementById('config').dataset
ReactDOM.render(
  <ApolloProvider client={client}><PointListWithPoint/></ApolloProvider>,
  document.getElementById('root')
);
